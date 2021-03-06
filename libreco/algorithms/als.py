#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""

References:
    [1] Haoming Li et al. Matrix Completion via Alternating Least Square(ALS)
        (https://stanford.edu/~rezab/classes/cme323/S15/notes/lec14.pdf)
    [2] Yifan Hu et al. Collaborative Filtering for Implicit Feedback Datasets
        (http://yifanhu.net/PUB/cf.pdf)
    [3] Gábor Takács et al. Applications of the Conjugate Gradient Method for Implicit Feedback Collaborative Filtering
        (http://citeseerx.ist.psu.edu/viewdoc/download?doi=10.1.1.379.6473&rep=rep1&type=pdf)

author: massquantity

"""
import time
import logging
from itertools import islice
from functools import partial
import numpy as np
from .base import Base
from ..evaluate.evaluate import EvalMixin
from ..utils.misc import time_block
from ..utils.initializers import truncated_normal
try:
    from ._als import als_update
except ImportError:
    LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logging.warn("Als cython version is not available")
    pass


class ALS(Base, EvalMixin):
    def __init__(
            self,
            task,
            data_info=None,
            embed_size=16,
            n_epochs=20,
            reg=None,
            alpha=10,
            seed=42,
            lower_upper_bound=None
    ):
        Base.__init__(self, task, data_info, lower_upper_bound)
        EvalMixin.__init__(self, task)

        self.task = task
        self.data_info = data_info
        self.embed_size = embed_size
        self.n_epochs = n_epochs
        self.reg = reg
        self.alpha = alpha
        self.seed = seed
        self.n_users = data_info.n_users
        self.n_items = data_info.n_items
        self.default_prediction = (
            data_info.global_mean
            if task == "rating"
            else 0.0
        )
        self.user_consumed = data_info.user_consumed
        self.user_embed = None
        self.item_embed = None

        self._build_model()
    #    print("Als init end..")

    def _build_model(self):
        np.random.seed(self.seed)
        self.user_embed = truncated_normal(
            shape=[self.n_users, self.embed_size], mean=0.0, scale=0.03)
        self.item_embed = truncated_normal(
            shape=[self.n_items, self.embed_size], mean=0.0, scale=0.03)

    def fit(self, train_data, verbose=1, shuffle=True, use_cg=True,
            n_threads=1, eval_data=None, metrics=None):
        self.show_start_time()
        user_interaction = train_data.sparse_interaction  # sparse.csr_matrix
        item_interaction = user_interaction.T.tocsr()
        if self.task == "ranking":
            self._check_has_sampled(train_data, verbose)
            user_interaction.data = user_interaction.data * self.alpha + 1
            item_interaction.data = item_interaction.data * self.alpha + 1
        trainer = self._choose_algo(use_cg)

        for epoch in range(1, self.n_epochs + 1):
            with time_block(f"Epoch {epoch}", verbose):
                trainer(interaction=user_interaction,
                        X=self.user_embed,
                        Y=self.item_embed,
                        reg=self.reg,
                        num_threads=n_threads)
                trainer(interaction=item_interaction,
                        X=self.item_embed,
                        Y=self.user_embed,
                        reg=self.reg,
                        num_threads=n_threads)

            if verbose > 1:
                self.print_metrics(eval_data=eval_data, metrics=metrics)
                print("="*30)

    def _choose_algo(self, use_cg):
        if self.task == "rating":
            if use_cg:
                trainer = partial(als_update, task="rating", use_cg=True)
            else:
                trainer = partial(als_update, task="rating", use_cg=False)
        elif self.task == "ranking":
            if use_cg:
                trainer = partial(als_update, task="ranking", use_cg=True)
            else:
                trainer = partial(als_update, task="ranking", use_cg=False)
        return trainer

    def predict(self, user, item):
        user = np.asarray(
            [user]) if isinstance(user, int) else np.asarray(user)
        item = np.asarray(
            [item]) if isinstance(item, int) else np.asarray(item)

        unknown_num, unknown_index, user, item = self._check_unknown(
            user, item
        )

        preds = np.sum(
            np.multiply(
                self.user_embed[user], self.item_embed[item]
            ),
            axis=1
        )

        if self.task == "rating":
            preds = np.clip(
                preds, self.lower_bound, self.upper_bound)
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
        recos = self.user_embed[user] @ self.item_embed.T
        if self.task == "ranking":
            recos = 1 / (1 + np.exp(-recos))

        ids = np.argpartition(recos, -count)[-count:]
        rank = sorted(zip(ids, recos[ids]), key=lambda x: -x[1])
        return list(
            islice(
                (rec for rec in rank if rec[0] not in consumed), n_rec
            )
        )


def _least_squares(sparse_interaction, X, Y, reg, embed_size, num, mode):
    indices = sparse_interaction.indices
    indptr = sparse_interaction.indptr
    data = sparse_interaction.data
    if mode == "explicit":
        for m in range(num):
            m_slice = slice(indptr[m], indptr[m + 1])
            interacted = Y[indices[m_slice]]
            labels = data[m_slice]
            A = interacted.T @ interacted + reg * np.eye(embed_size)
            b = interacted.T @ labels
            X[m] = np.linalg.solve(A, b)
    elif mode == "implicit":
        init_A = Y.T @ Y + reg * np.eye(embed_size, dtype=np.float32)
        for m in range(num):
            A = init_A.copy()
            b = np.zeros(embed_size, dtype=np.float32)
            for i in range(indptr[m], indptr[m+1]):
                factor = Y[indices[i]]
                confidence = data[i]
                # If confidence = 1, r_ui = 0 means no interaction.
                A += (confidence - 1) * np.outer(factor, factor)
                b += confidence * factor
            X[m] = np.linalg.solve(A, b)
    else:
        raise ValueError("mode must either be 'explicit' or 'implicit'")


# O(f^3) * m
def _least_squares_cg(sparse_interaction, X, Y, reg, embed_size, num,
                      mode, cg_steps=3):
    indices = sparse_interaction.indices
    indptr = sparse_interaction.indptr
    data = sparse_interaction.data
    if mode == "explicit":
        for m in range(num):
            m_slice = slice(indptr[m], indptr[m + 1])
            interacted = Y[indices[m_slice]]
            labels = data[m_slice]
            A = interacted.T @ interacted + reg * np.eye(embed_size)
            b = interacted.T @ labels
            X[m] = np.linalg.solve(A, b)
    elif mode == "implicit":
        init_A = Y.T @ Y + reg * np.eye(embed_size, dtype=np.float32)
        for m in range(num):
            x = X[m]
            r = -init_A @ x
            # compute r = b - Ax
            for i in range(indptr[m], indptr[m + 1]):
                y = Y[indices[i]]
                confidence = data[i]
                r += (confidence - (confidence - 1) * (y @ x)) * y

            p = r.copy()
            rs_old = r @ r
            if rs_old < 1e-10:
                continue

            for _ in range(cg_steps):
                Ap = init_A @ p
                for i in range(indptr[m], indptr[m+1]):
                    y = Y[indices[i]]
                    confidence = data[i]
                    Ap += (confidence - 1) * (y @ p) * y

                # standard CG update
                ak = rs_old / (p @ Ap)
                x += ak * p
                r -= ak * Ap
                rs_new = r @ r
                if rs_new < 1e-10:
                    break
                p = r + (rs_new / rs_old) * p
                rs_old = rs_new

            X[m] = x

    else:
        raise ValueError("mode must either be 'explicit' or 'implicit'")



