"""

References: Yehuda Koren "Matrix Factorization Techniques for Recommender Systems"
            (https://datajobs.com/data-science-repo/Recommender-Systems-[Netflix].pdf)

author: massquantity

"""
import time
from itertools import islice
import numpy as np
import tensorflow as tf2
from tensorflow.keras.initializers import (
    zeros as tf_zeros,
    truncated_normal as tf_truncated_normal
)
from .base import Base, TfMixin
from ..evaluate.evaluate import EvalMixin
from ..utils.tf_ops import reg_config
from ..utils.sampling import NegativeSampling
from ..data.data_generator import DataGenPure
tf = tf2.compat.v1
tf.disable_v2_behavior()


class SVD(Base, TfMixin, EvalMixin):
    def __init__(
            self,
            task,
            data_info,
            embed_size=16,
            n_epochs=20,
            lr=0.01,
            reg=None,
            batch_size=256,
            batch_sampling=False,
            num_neg=1,
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
        self.reg = reg_config(reg)
        self.batch_size = batch_size
        self.batch_sampling = batch_sampling
        self.num_neg = num_neg
        self.n_users = data_info.n_users
        self.n_items = data_info.n_items
        self.global_mean = data_info.global_mean
        self.default_prediction = data_info.global_mean if (
                task == "rating") else 0.0
        self.seed = seed
        self.user_consumed = data_info.user_consumed
        self.bu = None
        self.bi = None
        self.pu = None
        self.qi = None

        self._build_model()
        self._build_train_ops()

    def _build_model(self):
        self.user_indices = tf.placeholder(tf.int32, shape=[None])
        self.item_indices = tf.placeholder(tf.int32, shape=[None])
        self.labels = tf.placeholder(tf.float32, shape=[None])

        self.bu_var = tf.get_variable(name="bu_var", shape=[self.n_users],
                                      initializer=tf_zeros,
                                      regularizer=self.reg)
        self.bi_var = tf.get_variable(name="bi_var", shape=[self.n_items],
                                      initializer=tf_zeros,
                                      regularizer=self.reg)
        self.pu_var = tf.get_variable(name="pu_var",
                                      shape=[self.n_users, self.embed_size],
                                      initializer=tf_truncated_normal(
                                          0.0, 0.05),
                                      regularizer=self.reg)
        self.qi_var = tf.get_variable(name="pi_var",
                                      shape=[self.n_items, self.embed_size],
                                      initializer=tf_truncated_normal(
                                          0.0, 0.05),
                                      regularizer=self.reg)

        bias_user = tf.nn.embedding_lookup(self.bu_var, self.user_indices)
        bias_item = tf.nn.embedding_lookup(self.bi_var, self.item_indices)
        embed_user = tf.nn.embedding_lookup(self.pu_var, self.user_indices)
        embed_item = tf.nn.embedding_lookup(self.qi_var, self.item_indices)

        self.output = bias_user + bias_item + tf.reduce_sum(
            tf.multiply(embed_user, embed_item), axis=1)

    def _build_train_ops(self):
        if self.task == "rating":
            pred = self.output + self.global_mean
            self.loss = tf.losses.mean_squared_error(labels=self.labels,
                                                     predictions=pred)
        elif self.task == "ranking":
            # logits = tf.reshape(self.output, [-1])
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
        self.training_op = optimizer.minimize(total_loss)
        self.sess.run(tf.global_variables_initializer())

    def fit(self, train_data, verbose=1, shuffle=True,
            eval_data=None, metrics=None):
        self.show_start_time()
        if self.task == "ranking" and self.batch_sampling:
            self._check_has_sampled(train_data, verbose)
            data_generator = NegativeSampling(train_data,
                                              self.data_info,
                                              self.num_neg,
                                              batch_sampling=True)

        else:
            data_generator = DataGenPure(train_data)

        self.train_pure(data_generator, verbose, shuffle, eval_data, metrics)
        self._set_latent_factors()

    def predict(self, user, item):
        user = np.asarray(
            [user]) if isinstance(user, int) else np.asarray(user)
        item = np.asarray(
            [item]) if isinstance(item, int) else np.asarray(item)

        unknown_num, unknown_index, user, item = self._check_unknown(
            user, item)

        preds = self.bu[user] + self.bi[item] + np.sum(
            np.multiply(self.pu[user], self.qi[item]), axis=1)

        if self.task == "rating":
            preds = np.clip(
                preds + self.global_mean, self.lower_bound, self.upper_bound)
        elif self.task == "ranking":
            preds = 1 / (1 + np.exp(-preds))

        if unknown_num > 0:
            preds[unknown_index] = self.default_prediction

        return preds[0] if len(user) == 1 else preds

    def recommend_user(self, user, n_rec, **kwargs):
        user = self._check_unknown_user(user)
        if not user:
            return   # popular ?

        consumed = self.user_consumed[user]
        count = n_rec + len(consumed)
        recos = self.bu[user] + self.bi + self.pu[user] @ self.qi.T

        if self.task == "rating":
            recos += self.global_mean
        elif self.task == "ranking":
            recos = 1 / (1 + np.exp(-recos))
        ids = np.argpartition(recos, -count)[-count:]
        rank = sorted(zip(ids, recos[ids]), key=lambda x: -x[1])
        return list(
            islice(
                (rec for rec in rank if rec[0] not in consumed), n_rec
            )
        )

    def _set_latent_factors(self):
        self.bu, self.bi, self.pu, self.qi = self.sess.run(
            [self.bu_var, self.bi_var, self.pu_var, self.qi_var]
        )


