"""

Reference: Huifeng Guo et al. "DeepFM: A Factorization-Machine based Neural Network for CTR Prediction"
           (https://arxiv.org/pdf/1703.04247.pdf)

author: massquantity

"""
import time
from itertools import islice
import numpy as np
import tensorflow as tf2
from tensorflow.keras.initializers import (
    truncated_normal as tf_truncated_normal
)
from .base import Base, TfMixin
from ..evaluate.evaluate import EvalMixin
from ..utils.tf_ops import (
    reg_config,
    dropout_config,
    dense_nn,
    lr_decay_config
)
from ..data.data_generator import DataGenFeat
from ..utils.sampling import NegativeSampling
from ..utils.unique_features import (
    get_predict_indices_and_values,
    get_recommend_indices_and_values
)
tf = tf2.compat.v1
tf.disable_v2_behavior()


class DeepFM(Base, TfMixin, EvalMixin):

    def __init__(
            self,
            task,
            data_info=None,
            embed_size=16,
            n_epochs=20,
            lr=0.001,
            lr_decay=False,
            reg=None,
            batch_size=256,
            num_neg=1,
            use_bn=True,
            dropout_rate=None,
            hidden_units="128,64,32",
            batch_sampling=False,
            seed=42,
            lower_upper_bound=None,
            tf_sess_config=None
    ):
        Base.__init__(self, task, data_info, lower_upper_bound)
        TfMixin.__init__(self, tf_sess_config)
        EvalMixin.__init__(self, task)

        self.task = task
        self.data_info = data_info
        self.embed_size = embed_size
        self.n_epochs = n_epochs
        self.lr = lr
        self.lr_decay = lr_decay
        self.reg = reg_config(reg)
        self.batch_size = batch_size
        self.num_neg = num_neg
        self.use_bn = use_bn
        self.dropout_rate = dropout_config(dropout_rate)
        self.batch_sampling = batch_sampling
        self.hidden_units = list(map(int, hidden_units.split(",")))
        self.n_users = data_info.n_users
        self.n_items = data_info.n_items
        self.global_mean = data_info.global_mean
        self.default_prediction = data_info.global_mean if (
                task == "rating") else 0.0
        self.seed = seed
        self.user_consumed = data_info.user_consumed
        self.sparse = self._decide_sparse_indices(data_info)
        self.dense = self._decide_dense_values(data_info)
        if self.sparse:
            self.sparse_feature_size = self._sparse_feat_size(data_info)
            self.sparse_field_size = self._sparse_field_size(data_info)
        if self.dense:
            self.dense_field_size = self._dense_field_size(data_info)

    def _build_model(self):
        tf.set_random_seed(self.seed)
        self.labels = tf.placeholder(tf.float32, shape=[None])
        self.is_training = tf.placeholder_with_default(False, shape=[])
        self.linear_embed, self.pairwise_embed, self.deep_embed = [], [], []

        self._build_user_item()
        if self.sparse:
            self._build_sparse()
        if self.dense:
            self._build_dense()

        linear_embed = tf.concat(self.linear_embed, axis=1)
        pairwise_embed = tf.concat(self.pairwise_embed, axis=1)
        deep_embed = tf.concat(self.deep_embed, axis=1)

        linear_term = tf.layers.dense(linear_embed, units=1, activation=None)
        pairwise_term = 0.5 * tf.subtract(
            tf.square(tf.reduce_sum(pairwise_embed, axis=1)),
            tf.reduce_sum(tf.square(pairwise_embed), axis=1)
        )
        deep_term = dense_nn(deep_embed,
                             self.hidden_units,
                             use_bn=self.use_bn,
                             dropout_rate=self.dropout_rate,
                             is_training=self.is_training)

        concat_layer = tf.concat(
            [linear_term, pairwise_term, deep_term], axis=1)
        self.output = tf.squeeze(
            tf.layers.dense(concat_layer, units=1, activation=None))

    def _build_user_item(self):
        self.user_indices = tf.placeholder(tf.int32, shape=[None])
        self.item_indices = tf.placeholder(tf.int32, shape=[None])

        linear_user_feat = tf.get_variable(
            name="linear_user_feat",
            shape=[self.n_users, 1],
            initializer=tf_truncated_normal(0.0, 0.01),
            regularizer=self.reg)
        linear_item_feat = tf.get_variable(
            name="linear_item_feat",
            shape=[self.n_items, 1],
            initializer=tf_truncated_normal(0.0, 0.01),
            regularizer=self.reg)
        embed_user_feat = tf.get_variable(
            name="embed_user_feat",
            shape=[self.n_users, self.embed_size],
            initializer=tf_truncated_normal(0.0, 0.01),
            regularizer=self.reg)
        embed_item_feat = tf.get_variable(
            name="embed_item_feat",
            shape=[self.n_items, self.embed_size],
            initializer=tf_truncated_normal(0.0, 0.01),
            regularizer=self.reg)

        linear_user_embed = tf.nn.embedding_lookup(linear_user_feat,
                                                   self.user_indices)
        linear_item_embed = tf.nn.embedding_lookup(linear_item_feat,
                                                   self.item_indices)
        self.linear_embed.extend([linear_user_embed, linear_item_embed])

        pairwise_user_embed = tf.expand_dims(
            tf.nn.embedding_lookup(embed_user_feat, self.user_indices),
            axis=1)
        pairwise_item_embed = tf.expand_dims(
            tf.nn.embedding_lookup(embed_item_feat, self.item_indices),
            axis=1)
        self.pairwise_embed.extend([pairwise_user_embed, pairwise_item_embed])

        deep_user_embed = tf.nn.embedding_lookup(embed_user_feat,
                                                 self.user_indices)
        deep_item_embed = tf.nn.embedding_lookup(embed_item_feat,
                                                 self.item_indices)
        self.deep_embed.extend([deep_user_embed, deep_item_embed])

    def _build_sparse(self):
        self.sparse_indices = tf.placeholder(
            tf.int32, shape=[None, self.sparse_field_size])

        linear_sparse_feat = tf.get_variable(
            name="linear_sparse_feat",
            shape=[self.sparse_feature_size],
            initializer=tf_truncated_normal(0.0, 0.01),
            regularizer=self.reg)
        embed_sparse_feat = tf.get_variable(
            name="embed_sparse_feat",
            shape=[self.sparse_feature_size, self.embed_size],
            initializer=tf_truncated_normal(0.0, 0.01),
            regularizer=self.reg)

        linear_sparse_embed = tf.nn.embedding_lookup(    # B * F1
            linear_sparse_feat, self.sparse_indices)
        pairwise_sparse_embed = tf.nn.embedding_lookup(  # B * F1 * K
            embed_sparse_feat, self.sparse_indices)
        deep_sparse_embed = tf.reshape(
            pairwise_sparse_embed,
            [-1, self.sparse_field_size * self.embed_size]
        )
        self.linear_embed.append(linear_sparse_embed)
        self.pairwise_embed.append(pairwise_sparse_embed)
        self.deep_embed.append(deep_sparse_embed)

    def _build_dense(self):
        self.dense_values = tf.placeholder(
            tf.float32, shape=[None, self.dense_field_size])
        dense_values_reshape = tf.reshape(
            self.dense_values, [-1, self.dense_field_size, 1])
        batch_size = tf.shape(self.dense_values)[0]

        linear_dense_feat = tf.get_variable(
            name="linear_dense_feat",
            shape=[self.dense_field_size],
            initializer=tf_truncated_normal(0.0, 0.01),
            regularizer=self.reg)
        embed_dense_feat = tf.get_variable(
            name="embed_dense_feat",
            shape=[self.dense_field_size, self.embed_size],
            initializer=tf_truncated_normal(0.0, 0.01),
            regularizer=self.reg)

        # B * F2
        linear_dense_embed = tf.tile(linear_dense_feat, [batch_size])
        linear_dense_embed = tf.reshape(
            linear_dense_embed, [-1, self.dense_field_size])
        linear_dense_embed = tf.multiply(
            linear_dense_embed, self.dense_values)

        pairwise_dense_embed = tf.expand_dims(embed_dense_feat, axis=0)
        # B * F2 * K
        pairwise_dense_embed = tf.tile(
            pairwise_dense_embed, [batch_size, 1, 1])
        pairwise_dense_embed = tf.multiply(
            pairwise_dense_embed, dense_values_reshape)

        deep_dense_embed = tf.reshape(
            pairwise_dense_embed,
            [-1, self.dense_field_size * self.embed_size]
        )

        self.linear_embed.append(linear_dense_embed)
        self.pairwise_embed.append(pairwise_dense_embed)
        self.deep_embed.append(deep_dense_embed)

    def _build_train_ops(self, global_steps=None):
        if self.task == "rating":
            self.loss = tf.losses.mean_squared_error(labels=self.labels,
                                                     predictions=self.output)
        elif self.task == "ranking":
            self.loss = tf.reduce_mean(
                tf.nn.sigmoid_cross_entropy_with_logits(labels=self.labels,
                                                        logits=self.output)
            )

        if self.reg is not None:
            reg_keys = tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)
            total_loss = self.loss + tf.add_n(reg_keys)
        else:
            total_loss = self.loss

        optimizer = tf.train.AdamOptimizer(self.lr)
        optimizer_op = optimizer.minimize(total_loss, global_step=global_steps)
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        self.training_op = tf.group([optimizer_op, update_ops])
        self.sess.run(tf.global_variables_initializer())

    def fit(self, train_data, verbose=1, shuffle=True,
            eval_data=None, metrics=None, **kwargs):
        self.show_start_time()
        if self.lr_decay:
            n_batches = int(len(train_data) / self.batch_size)
            self.lr, global_steps = lr_decay_config(self.lr, n_batches,
                                                    **kwargs)
        else:
            global_steps = None

        self._build_model()
        self._build_train_ops(global_steps)

        if self.task == "ranking" and self.batch_sampling:
            self._check_has_sampled(train_data, verbose)
            data_generator = NegativeSampling(train_data,
                                              self.data_info,
                                              self.num_neg,
                                              self.sparse,
                                              self.dense,
                                              batch_sampling=True)

        else:
            data_generator = DataGenFeat(train_data,
                                         self.sparse,
                                         self.dense)

        self.train_feat(data_generator, verbose, shuffle, eval_data, metrics)

    def predict(self, user, item):
        user = np.asarray(
            [user]) if isinstance(user, int) else np.asarray(user)
        item = np.asarray(
            [item]) if isinstance(item, int) else np.asarray(item)

        unknown_num, unknown_index, user, item = self._check_unknown(
            user, item)

        (user_indices,
         item_indices,
         sparse_indices,
         dense_values) = get_predict_indices_and_values(
            self.data_info, user, item, self.n_items, self.sparse, self.dense)
        feed_dict = self._get_feed_dict(user_indices, item_indices,
                                        sparse_indices, dense_values,
                                        None, False)

        preds = self.sess.run(self.output, feed_dict)
        if self.task == "rating":
            preds = np.clip(preds, self.lower_bound, self.upper_bound)
        elif self.task == "ranking":
            preds = 1 / (1 + np.exp(-preds))

        if unknown_num > 0:
            preds[unknown_index] = self.default_prediction

        return preds

    def recommend_user(self, user, n_rec, **kwargs):
        user = self._check_unknown_user(user)
        if not user:
            return   # popular ?

        (user_indices,
         item_indices,
         sparse_indices,
         dense_values) = get_recommend_indices_and_values(
            self.data_info, user, self.n_items, self.sparse, self.dense)
        feed_dict = self._get_feed_dict(user_indices, item_indices,
                                        sparse_indices, dense_values,
                                        None, False)

        recos = self.sess.run(self.output, feed_dict)
        if self.task == "ranking":
            recos = 1 / (1 + np.exp(-recos))

        consumed = self.user_consumed[user]
        count = n_rec + len(consumed)
        ids = np.argpartition(recos, -count)[-count:]
        rank = sorted(zip(ids, recos[ids]), key=lambda x: -x[1])
        return list(
            islice(
                (rec for rec in rank if rec[0] not in consumed), n_rec
            )
        )


