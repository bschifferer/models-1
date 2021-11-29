import merlin_standard_lib as msl
import tensorflow as tf
from merlin_standard_lib import Schema, Tag

import merlin_models.tf as ml
from merlin_models.data.synthetic import generate_recsys_data

synthetic_music_recsys_data_schema = Schema(
    [
        # Item
        msl.ColumnSchema.create_categorical(
            "item_id",
            num_items=10000,
            tags=[Tag.ITEM_ID],
        ),
        msl.ColumnSchema.create_categorical(
            "item_category",
            num_items=100,
            tags=[Tag.ITEM],
        ),
        msl.ColumnSchema.create_continuous(
            "item_recency",
            min_value=0,
            max_value=1,
            tags=[Tag.ITEM],
        ),
        msl.ColumnSchema.create_categorical(
            "item_genres",
            num_items=100,
            value_count=msl.schema.ValueCount(1, 20),
            tags=[Tag.ITEM],
        ),
        # User
        msl.ColumnSchema.create_categorical(
            "country",
            num_items=100,
            tags=[Tag.USER],
        ),
        msl.ColumnSchema.create_continuous(
            "user_age",
            is_float=False,
            min_value=18,
            max_value=50,
            tags=[Tag.USER],
        ),
        msl.ColumnSchema.create_categorical(
            "user_genres",
            num_items=100,
            value_count=msl.schema.ValueCount(1, 20),
        ),
        # Bias
        msl.ColumnSchema.create_continuous(
            "position",
            is_float=False,
            min_value=1,
            max_value=100,
            tags=["bias"],
        ),
        # Targets
        msl.ColumnSchema("click").with_tags(tags=[Tag.BINARY_CLASSIFICATION]),
        msl.ColumnSchema("play").with_tags(tags=[Tag.BINARY_CLASSIFICATION]),
        msl.ColumnSchema("like").with_tags(tags=[Tag.BINARY_CLASSIFICATION]),
    ]
)

# RETRIEVAL


def build_matrix_factorization(schema: Schema, dim=128):
    model = ml.MatrixFactorizationBlock(schema, dim).to_model(schema)

    return model


def build_youtube_dnn(schema: Schema, dims=(512, 256), num_sampled=50) -> ml.Model:
    user_schema = schema.select_by_tag(Tag.USER)
    dnn = ml.inputs(user_schema, post="continuous-powers").apply(ml.MLPBlock(dims))
    prediction_task = ml.SampledItemPredictionTask(schema, dim=dims[-1], num_sampled=num_sampled)

    model = dnn.to_model(prediction_task)

    return model


def build_two_tower(schema: Schema, target="play", dims=(512, 256)) -> ml.Model:
    def method_1() -> ml.Model:
        return ml.TwoTowerBlock(schema, ml.MLPBlock(dims)).to_model(schema.select_by_name(target))

    def method_2() -> ml.Model:
        user_tower = ml.inputs(schema.select_by_tag(Tag.USER), ml.MLPBlock([512, 256]))
        item_tower = ml.inputs(schema.select_by_tag(Tag.ITEM), ml.MLPBlock([512, 256]))
        two_tower = ml.merge({"user": user_tower, "item": item_tower}, aggregation="cosine")
        model = two_tower.to_model(schema.select_by_name(target))

        return model

    def method_3() -> ml.Model:
        def routes_verbose(inputs, schema: Schema):
            user_features = schema.select_by_tag(Tag.USER).filter_columns_from_dict(inputs)
            item_features = schema.select_by_tag(Tag.ITEM).filter_columns_from_dict(inputs)

            user_tower = ml.MLPBlock(dims)(user_features)
            item_tower = ml.MLPBlock(dims)(item_features)

            return ml.ParallelBlock(dict(user=user_tower, item=item_tower), aggregation="cosine")

        user_tower = ml.MLPBlock(dims, filter=Tag.USER).as_tabular("user")
        item_tower = ml.MLPBlock(dims, filter=Tag.ITEM).as_tabular("item")

        two_tower = ml.inputs(schema).branch(user_tower, item_tower, aggregation="cosine")
        model = two_tower.to_model(schema.select_by_name(target))

        return model

    return method_2()


# RANKING


def build_dnn(schema: Schema, residual=False) -> ml.Model:
    bias_block = ml.MLPBlock([256, 128]).from_inputs(schema.select_by_tag("bias"))
    schema = schema.remove_by_tag("bias")

    if residual:
        block = ml.inputs(schema, ml.DenseResidualBlock(depth=2))
    else:
        block = ml.inputs(schema, ml.MLPBlock([512, 256]))

    return block.to_model(schema, bias_block=bias_block)


def build_dcn(schema: Schema) -> ml.Model:
    schema = schema.remove_by_tag("bias")

    # deep_cross = ml.inputs(schema, ml.CrossBlock(3)).apply(ml.MLPBlock([512, 256]))

    deep_cross = ml.inputs(schema).branch(
        ml.CrossBlock(3), ml.MLPBlock([512, 256]), aggregation="concat"
    )

    # deep_cross = ml.inputs(schema, ml.CrossBlock(3))
    # deep_cross = deep_cross.apply_with_shortcut(ml.MLPBlock([512, 256]), aggregation="concat")

    return deep_cross.to_model(schema)


def build_advanced_ranking_model(schema: Schema, head="ple") -> ml.Model:
    # TODO: Change msl to be able to make this a single function call.
    bias_block = ml.MLPBlock([512, 256]).from_inputs(schema.select_by_tag("bias"))
    body = ml.DLRMBlock(
        schema.remove_by_tag("bias"),
        bottom_block=ml.MLPBlock([512, 128]),
        top_block=ml.MLPBlock([128, 64]),
    )

    # expert_block, output_names = ml.MLPBlock([64, 32]), ml.Head.task_names_from_schema(schema)
    # mmoe = ml.MMOE(expert_block, num_experts=3, output_names=output_names)
    # model = body.add(mmoe).to_model(schema)

    if head == "mmoe":
        return ml.MMOEHead.from_schema(
            schema,
            body,
            task_blocks=ml.MLPBlock([64, 32]),
            expert_block=ml.MLPBlock([64, 32]),
            bias_block=bias_block,
            num_experts=3,
        ).to_model()
    elif head == "ple":
        return ml.PLEHead.from_schema(
            schema,
            body,
            task_blocks=ml.MLPBlock([64, 32]),
            expert_block=ml.MLPBlock([64, 32]),
            num_shared_experts=2,
            num_task_experts=2,
            depth=2,
            bias_block=bias_block,
        ).to_model()

    return body.to_model(schema)


def build_dlrm(schema: Schema) -> ml.Model:
    model: ml.Model = ml.DLRMBlock(
        schema, bottom_block=ml.MLPBlock([512, 128]), top_block=ml.MLPBlock([512, 128])
    ).to_model(schema)

    return model


def data_from_schema(schema, num_items=1000, next_item_prediction=False) -> tf.data.Dataset:
    data_df = generate_recsys_data(num_items, schema)

    if next_item_prediction:
        targets = {"item_id": data_df.pop("item_id")}
    else:
        targets = {}
        for target in synthetic_music_recsys_data_schema.select_by_tag(Tag.BINARY_CLASSIFICATION):
            targets[target.name] = data_df.pop(target.name)

    dataset = tf.data.Dataset.from_tensor_slices((dict(data_df), targets))

    return dataset


if __name__ == "__main__":
    dataset = data_from_schema(synthetic_music_recsys_data_schema).batch(100)
    # model = build_dnn(synthetic_music_recsys_data_schema, residual=True)
    # model = build_advanced_ranking_model(synthetic_music_recsys_data_schema)
    # model = build_dcn(synthetic_music_recsys_data_schema)
    # model = build_dlrm(synthetic_music_recsys_data_schema)
    model = build_two_tower(synthetic_music_recsys_data_schema, target="play")

    # dataset = data_from_schema(synthetic_music_recsys_data_schema,
    #                            next_item_prediction=True).batch(100)
    # model = build_youtube_dnn(synthetic_music_recsys_data_schema)

    model.compile(optimizer="adam", run_eagerly=True)

    inputs, targets = [i for i in dataset.as_numpy_iterator()][0]

    # TODO: remove this after fix in T4Rec
    predictions = model(inputs)
    # loss = model.compute_loss(predictions, targets)

    model.fit(dataset)

    a = 5