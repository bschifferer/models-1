from typing import List, Optional

import tensorflow as tf
from nvtabular.column_group import ColumnGroup


def calculate_batch_size_from_input_shapes(input_shapes):
    return [i for i in input_shapes.values() if not isinstance(i, tuple)][0][0]


class FilterFeatures(tf.keras.layers.Layer):
    def __init__(
        self, columns, trainable=False, name=None, dtype=None, dynamic=False, pop=False, **kwargs
    ):
        super().__init__(trainable, name, dtype, dynamic, **kwargs)
        self.columns = columns
        self.pop = pop

    def call(self, inputs, **kwargs):
        assert isinstance(inputs, dict), "Inputs needs to be a dict"

        outputs = {k: v for k, v in inputs.items() if k in self.columns}
        if self.pop:
            for key in outputs.keys():
                inputs.pop(key)

        return outputs

    def compute_output_shape(self, input_shape):
        return {k: v for k, v in input_shape.items() if k in self.columns}

    def get_config(self):
        return {
            "columns": self.columns,
        }


class ConcatFeatures(tf.keras.layers.Layer):
    def __init__(self, axis=-1, trainable=False, name=None, dtype=None, dynamic=False, **kwargs):
        super().__init__(trainable, name, dtype, dynamic, **kwargs)
        self.axis = axis
        self.flatten = tf.keras.layers.Flatten()

    def call(self, inputs, **kwargs):
        return tf.concat(
            tf.nest.flatten(tf.nest.map_structure(self.flatten, inputs)), axis=self.axis
        )

    def compute_output_shape(self, input_shapes):
        batch_size = calculate_batch_size_from_input_shapes(input_shapes)

        return batch_size, sum([i[1] for i in input_shapes.values()])

    def repr_ignore(self) -> List[str]:
        return ["flatten"]

    def get_config(self):
        return {
            "axis": self.axis,
        }


class StackFeatures(tf.keras.layers.Layer):
    def __init__(self, axis=-1, trainable=False, name=None, dtype=None, dynamic=False, **kwargs):
        super().__init__(trainable, name, dtype, dynamic, **kwargs)
        self.axis = axis
        self.flatten = tf.keras.layers.Flatten()

    def call(self, inputs, **kwargs):
        return tf.stack(
            tf.nest.flatten(tf.nest.map_structure(self.flatten, inputs)), axis=self.axis
        )

    def compute_output_shape(self, input_shapes):
        batch_size = calculate_batch_size_from_input_shapes(input_shapes)
        last_dim = [i for i in input_shapes.values()][0][-1]

        return batch_size, len(input_shapes), last_dim

    def repr_ignore(self) -> List[str]:
        return ["flatten"]

    def get_config(self):
        return {
            "axis": self.axis,
        }


class AsTabular(tf.keras.layers.Layer):
    def __init__(
        self, output_name, trainable=False, name=None, dtype=None, dynamic=False, **kwargs
    ):
        super().__init__(trainable, name, dtype, dynamic, **kwargs)
        self.output_name = output_name

    def call(self, inputs, **kwargs):
        return {self.output_name: inputs}

    def get_config(self):
        return {
            "axis": self.axis,
        }


class TabularLayer(tf.keras.layers.Layer):
    def __init__(
        self, aggregation=None, trainable=True, name=None, dtype=None, dynamic=False, **kwargs
    ):
        super().__init__(trainable, name, dtype, dynamic, **kwargs)
        self.set_aggregation(aggregation)

    def set_aggregation(self, aggregation):
        if aggregation == "concat":
            self.aggregation = ConcatFeatures()
        elif aggregation == "stack":
            self.aggregation = StackFeatures()
        else:
            self.aggregation = None

    def __call__(
        self,
        inputs,
        pre=None,
        post=None,
        merge_with=None,
        stack_outputs=False,
        concat_outputs=False,
        filter_columns=None,
        training=False,
        **kwargs
    ):
        post_op = getattr(self, "aggregation", None)
        if concat_outputs:
            post_op = ConcatFeatures()
        if stack_outputs:
            post_op = StackFeatures()
        if filter_columns:
            pre = FilterFeatures(filter_columns)
        if pre:
            inputs = pre(inputs)
        outputs = super().__call__(inputs, training=training, **kwargs)

        if merge_with:
            if not isinstance(merge_with, list):
                merge_with = [merge_with]
            for layer_or_tensor in merge_with:
                to_add = (
                    layer_or_tensor(inputs, training=training, **kwargs)
                    if callable(layer_or_tensor)
                    else layer_or_tensor
                )
                outputs.update(to_add)

        if post_op:
            outputs = post_op(outputs)

        return outputs

    def compute_output_shape(self, input_shapes):
        if self.aggregation:
            return self.aggregation.compute_output_shape(input_shapes)

        return input_shapes

    def apply_to_all(self, inputs, columns_to_filter=None):
        if columns_to_filter:
            inputs = FilterFeatures(columns_to_filter)(inputs)
        outputs = tf.nest.map_structure(self, inputs)

        return outputs

    def repr_ignore(self):
        return []

    def repr_extra(self):
        return []

    def repr_add(self):
        return []

    def calculate_batch_size_from_input_shapes(self, input_shapes):
        return calculate_batch_size_from_input_shapes(input_shapes)

    @classmethod
    def from_column_group(
        cls, column_group: ColumnGroup, tags=None, tags_to_filter=None, **kwargs
    ) -> Optional["TabularLayer"]:
        if tags:
            column_group = column_group.get_tagged(tags, tags_to_filter=tags_to_filter)

        if not column_group.columns:
            return None

        return cls.from_features(column_group.columns, **kwargs)

    @classmethod
    def from_features(cls, features, **kwargs):
        return features >> cls(**kwargs)

    def __rrshift__(self, other):
        from merlin_models.tf.blocks.base import right_shift_layer

        return right_shift_layer(self, other)


class MergeTabular(TabularLayer):
    def __init__(
        self,
        *to_merge,
        aggregation=None,
        trainable=True,
        name=None,
        dtype=None,
        dynamic=False,
        **kwargs
    ):
        super().__init__(aggregation, trainable, name, dtype, dynamic, **kwargs)
        self.to_merge = to_merge

    def call(self, inputs, **kwargs):
        assert isinstance(inputs, dict), "Inputs needs to be a dict"

        outputs = {}
        for layer in self.to_merge:
            outputs.update(layer(inputs))

        return outputs

    def compute_output_shape(self, input_shape):
        output_shapes = {}

        for layer in self.to_merge:
            output_shapes.update(layer.compute_output_shape(input_shape))

        return output_shapes

    def get_config(self):
        return {"merge_layers": tf.keras.utils.serialize_keras_object(self.merge_layers)}


def merge_tabular(self, other, **kwargs):
    return MergeTabular(self, other)


TabularLayer.__add__ = merge_tabular
TabularLayer.merge = merge_tabular


class AsSparseFeatures(TabularLayer):
    def call(self, inputs, **kwargs):
        outputs = {}
        for name, val in inputs.items():
            if isinstance(val, tuple):
                values = val[0][:, 0]
                row_lengths = val[1][:, 0]
                outputs[name] = tf.RaggedTensor.from_row_lengths(values, row_lengths).to_sparse()
            else:
                outputs[name] = val

        return outputs

    def compute_output_shape(self, input_shape):
        return {k: v for k, v in input_shape.items() if k in self.columns}


class AsDenseFeatures(TabularLayer):
    def call(self, inputs, **kwargs):
        outputs = {}
        for name, val in inputs.items():
            if isinstance(val, tuple):
                values = val[0][:, 0]
                row_lengths = val[1][:, 0]
                outputs[name] = tf.RaggedTensor.from_row_lengths(values, row_lengths).to_tensor()
            else:
                outputs[name] = val

        return outputs


class ParseTokenizedText(TabularLayer):
    def __init__(
        self,
        max_text_length=None,
        aggregation=None,
        trainable=True,
        name=None,
        dtype=None,
        dynamic=False,
        **kwargs
    ):
        super().__init__(aggregation, trainable, name, dtype, dynamic, **kwargs)
        self.max_text_length = max_text_length

    def call(self, inputs, **kwargs):
        outputs, text_tensors, text_column_names = {}, {}, []
        for name, val in inputs.items():
            if isinstance(val, tuple) and name.endswith(("/tokens", "/attention_mask")):
                values = val[0][:, 0]
                row_lengths = val[1][:, 0]
                text_tensors[name] = tf.RaggedTensor.from_row_lengths(
                    values, row_lengths
                ).to_tensor()
                text_column_names.append("/".join(name.split("/")[:-1]))
            # else:
            #     outputs[name] = val

        for text_col in set(text_column_names):
            outputs[text_col] = dict(
                input_ids=tf.cast(text_tensors[text_col + "/tokens"], tf.int32),
                attention_mask=tf.cast(text_tensors[text_col + "/attention_mask"], tf.int32),
            )

        return outputs

    def compute_output_shape(self, input_shapes):
        assert self.max_text_length is not None

        output_shapes, text_column_names = {}, []
        batch_size = self.calculate_batch_size_from_input_shapes(input_shapes)
        for name, val in input_shapes.items():
            if isinstance(val, tuple) and name.endswith(("/tokens", "/attention_mask")):
                text_column_names.append("/".join(name.split("/")[:-1]))

        for text_col in set(text_column_names):
            output_shapes[text_col] = dict(
                input_ids=tf.TensorShape([batch_size, self.max_text_length]),
                attention_mask=tf.TensorShape([batch_size, self.max_text_length]),
            )

        return output_shapes