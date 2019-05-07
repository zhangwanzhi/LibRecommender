import time
import numpy as np
import tensorflow as tf
from ..utils.sampling import pairwise_sampling
from ..utils.initializers import truncated_normal
from ..evaluate.evaluate import precision_tf, AP_at_k, MAP_at_k, HitRatio_at_k, NDCG_at_k, binary_cross_entropy
from sklearn.metrics import roc_auc_score, average_precision_score


class BPR:
    def __init__(self, n_factors=16, lr=0.01, n_epochs=20, reg=0.0,
                 iteration=1000, batch_size=64, seed=42):
        self.n_factors = n_factors
        self.lr = lr
        self.n_epochs = n_epochs
        self.reg = reg
        self.iteration = iteration
        self.batch_size = batch_size
        self.seed = seed

    def build_model(self, dataset):
        np.random.seed(self.seed)
        self.pu = truncated_normal(shape=[dataset.n_users, self.n_factors], mean=0.0, scale=0.05)
        self.qi = truncated_normal(shape=[dataset.n_items, self.n_factors], mean=0.0, scale=0.05)

    def fit(self, dataset, sampling_mode="batch", verbose=1):
        if verbose > 0:
            sampling = pairwise_sampling(dataset)
            train_user, train_item, train_label = sampling(mode="train")
            test_user, test_item, test_label = sampling(mode="test")

        self.dataset = dataset
        self.build_model(self.dataset)
        if sampling_mode == "bootstrap":
            sampling = pairwise_sampling(self.dataset)
            t0 = time.time()
            for i in range(1, self.iteration + 1):
                user, item_i, item_j, x_uij = sampling.next_mf(self.pu, self.qi, bootstrap=True)
                sigmoid = 1.0 / (1.0 + np.exp(x_uij))
                self.pu[user] += self.lr * (sigmoid * (self.qi[item_i] - self.qi[item_j]) +
                                            self.reg * self.pu[user])
                self.qi[item_i] += self.lr * (sigmoid * self.pu[user] + self.reg * self.qi[item_i])
                self.qi[item_j] += self.lr * (sigmoid * (-self.pu[user]) + self.reg * self.qi[item_j])

                if i % len(self.dataset.train_user_indices) == 0 and verbose > 0:
                    print("iteration {}, time: {:.2f}".format(i, time.time() - t0))
                    t0 = time.time()
                    train_loss, train_prob = binary_cross_entropy(self, train_user, train_item, train_label)
                    train_roc_auc = roc_auc_score(train_label, train_prob)
                    train_pr_auc = average_precision_score(train_label, train_prob)
                    print("train loss: {:.2f}, train roc auc: {:.2f}, train pr auc: {:.2f}".format(
                        train_loss, train_roc_auc, train_pr_auc))

                    test_loss, test_prob = binary_cross_entropy(self, test_user, test_item, test_label)
                    test_roc_auc = roc_auc_score(test_label, test_prob)
                    test_pr_auc = average_precision_score(test_label, test_prob)
                    print("test loss: {:.2f}, test auc: {:.2f}, test pr auc: {:.2f}".format(
                        test_loss, test_roc_auc, test_pr_auc))
                    print()

        elif sampling_mode == "sgd":
            for epoch in range(1, self.n_epochs + 1):
                t0 = time.time()
                sampling = pairwise_sampling(self.dataset, batch_size=1)
                for _ in range(len(self.dataset.train_user_indices)):
                    user, item_i, item_j, x_uij = sampling.next_mf(self.pu, self.qi, bootstrap=False)
                    sigmoid = 1.0 / (1.0 + np.exp(x_uij))
                    self.pu[user] += self.lr * (sigmoid * (self.qi[item_i] - self.qi[item_j]) +
                                                self.reg * self.pu[user])
                    self.qi[item_i] += self.lr * (sigmoid * self.pu[user] + self.reg * self.qi[item_i])
                    self.qi[item_j] += self.lr * (sigmoid * (-self.pu[user]) + self.reg * self.qi[item_j])

                if verbose > 0:
                    print("Epoch {}, fit time: {:.2f}".format(epoch, time.time() - t0))
                    train_loss, train_prob = binary_cross_entropy(self, train_user, train_item, train_label)
                    train_roc_auc = roc_auc_score(train_label, train_prob)
                    train_pr_auc = average_precision_score(train_label, train_prob)
                    print("train loss: {:.2f}, train roc auc: {:.2f}, train pr auc: {:.2f}".format(
                        train_loss, train_roc_auc, train_pr_auc))

                    test_loss, test_prob = binary_cross_entropy(self, test_user, test_item, test_label)
                    test_roc_auc = roc_auc_score(test_label, test_prob)
                    test_pr_auc = average_precision_score(test_label, test_prob)
                    print("test loss: {:.2f}, test auc: {:.2f}, test pr auc: {:.2f}".format(
                        test_loss, test_roc_auc, test_pr_auc))
                    print()

        elif sampling_mode == "batch":
            for epoch in range(1, self.n_epochs + 1):
                t0 = time.time()
                sampling = pairwise_sampling(self.dataset, batch_size=self.batch_size)
                n_batches = len(self.dataset.train_user_indices) // self.batch_size
                for n in range(n_batches):
                    batch_user, \
                    batch_item_i, \
                    batch_item_j, \
                    batch_x_uij = sampling.next_mf(self.pu, self.qi, bootstrap=False)

                    sigmoids = 1.0 / (1.0 + np.exp(batch_x_uij))
                    sigmoids = sigmoids.reshape(self.batch_size, 1)
                    self.pu[batch_user] += self.lr * (
                            sigmoids * (
                                self.qi[batch_item_i] - self.qi[batch_item_j]) +
                                    self.reg * self.pu[batch_user])
                    self.qi[batch_item_i] += self.lr * (sigmoids * self.pu[batch_user] +
                                                        self.reg * self.qi[batch_item_i])
                    self.qi[batch_item_j] += self.lr * (sigmoids * (-self.pu[batch_user]) +
                                                        self.reg * self.qi[batch_item_j])

                if verbose > 0:
                    print("Epoch {}, fit time: {:.2f}".format(epoch, time.time() - t0))
                    train_loss, train_prob = binary_cross_entropy(self, train_user, train_item, train_label)
                    train_roc_auc = roc_auc_score(train_label, train_prob)
                    train_pr_auc = average_precision_score(train_label, train_prob)
                    print("train loss: {:.2f}, train roc auc: {:.2f}, train pr auc: {:.2f}".format(
                        train_loss, train_roc_auc, train_pr_auc))

                    test_loss, test_prob = binary_cross_entropy(self, test_user, test_item, test_label)
                    test_roc_auc = roc_auc_score(test_label, test_prob)
                    test_pr_auc = average_precision_score(test_label, test_prob)
                    print("test loss: {:.2f}, test auc: {:.2f}, test pr auc: {:.2f}".format(
                        test_loss, test_roc_auc, test_pr_auc))
                    print()

        else:
            raise ValueError("Sampling Mode must be one of these: bootstrap, sgd, batch")

    def predict(self, u, i):
        try:
            logits = np.dot(self.pu[u], self.qi[i])
            prob = 1.0 / (1.0 + np.exp(-logits))
            pred = float(np.where(prob >= 0.5, 1.0, 0.0))
        except IndexError:
            prob, pred = 0.0, 0.0
        return prob, pred



class BPR_tf:
    def __init__(self, n_factors=16, lr=0.01, n_epochs=20, reg=0.0,
                 iteration=1000, batch_size=64, seed=42):
        self.n_factors = n_factors
        self.lr = lr
        self.n_epochs = n_epochs
        self.reg = reg
        self.iteration = iteration
        self.batch_size = batch_size
        self.seed = seed

    def build_model(self, dataset):
        tf.set_random_seed(self.seed)
        self.dataset = dataset
        self.user = tf.placeholder(tf.int32, shape=[None], name="user")
        self.item_i = tf.placeholder(tf.int32, shape=[None], name="item_i")
        self.item_j = tf.placeholder(tf.int32, shape=[None], name="item_j")

        self.pu = tf.Variable(tf.truncated_normal([dataset.n_users, self.n_factors], mean=0.0, stddev=0.05))
        self.qi = tf.Variable(tf.truncated_normal([dataset.n_items, self.n_factors], mean=0.0, stddev=0.05))
        self.embed_user = tf.nn.embedding_lookup(self.pu, self.user)
        self.embed_item_i = tf.nn.embedding_lookup(self.qi, self.item_i)
        self.embed_item_j = tf.nn.embedding_lookup(self.qi, self.item_j)

        self.x_ui = tf.reduce_sum(tf.multiply(self.embed_user, self.embed_item_i), axis=1)
        self.x_uj = tf.reduce_sum(tf.multiply(self.embed_user, self.embed_item_j), axis=1)
        self.x_uij = self.x_ui - self.x_uj

        self.reg_user = self.reg * tf.nn.l2_loss(self.embed_user)
        self.reg_item_i = self.reg * tf.nn.l2_loss(self.embed_item_i)
        self.reg_item_j = self.reg * tf.nn.l2_loss(self.embed_item_j)
        self.loss = - tf.reduce_sum(
            tf.log(1 / (1 + tf.exp(-self.x_uij))) - self.reg_user - self.reg_item_i - self.reg_item_j)

        self.item_t = tf.placeholder(tf.int32, shape=[None])
        self.embed_item_t = tf.nn.embedding_lookup(self.qi, self.item_t)
        self.logits = tf.reduce_sum(tf.multiply(self.embed_user, self.embed_item_t), axis=1)
        self.prob = tf.sigmoid(self.logits)

    def fit(self, dataset, verbose=1):
        if verbose > 0:
            sampling = pairwise_sampling(dataset)
            train_user, train_item, train_label = sampling(mode="train")
            test_user, test_item, test_label = sampling(mode="test")

        self.build_model(dataset)
    #    self.optimizer = tf.train.AdamOptimizer(self.lr)
        self.optimizer = tf.train.FtrlOptimizer(learning_rate=0.1, l1_regularization_strength=1e-3)
        self.training_op = self.optimizer.minimize(self.loss)
        init = tf.global_variables_initializer()
        self.sess = tf.Session()
        self.sess.run(init)
        with self.sess.as_default():
            for epoch in range(1, self.n_epochs + 1):
                t0 = time.time()
                sampling = pairwise_sampling(self.dataset, batch_size=self.batch_size)
                n_batches = len(dataset.train_user_indices) // self.batch_size
                for n in range(n_batches):
                    batch_user, \
                    batch_item_i, \
                    batch_item_j = sampling.next_mf_tf()

                    self.sess.run(self.training_op, feed_dict={self.user: batch_user,
                                                               self.item_i: batch_item_i,
                                                               self.item_j: batch_item_j})

                if verbose > 0:
                    print("Epoch {}, fit time: {:.2f}".format(epoch, time.time() - t0))
                    '''
                    train_loss, train_prob = self.sess.run([self.loss, self.prob],
                                                           feed_dict={self.user: train_user,
                                                                      self.item_t: train_item,
                                                                      self.item_i: np.zeros(train_item.shape),
                                                                      self.item_j: np.zeros(train_item.shape)})
                    train_roc_auc = roc_auc_score(train_label, train_prob)
                    train_pr_auc = average_precision_score(train_label, train_prob)
                    print("train loss: {:.2f}, train roc auc: {:.2f}, train pr auc: {:.2f}".format(
                        train_loss, train_roc_auc, train_pr_auc))
                    '''
                    test_loss, test_prob = self.sess.run([self.loss, self.prob],
                                                           feed_dict={self.user: test_user,
                                                                      self.item_t: test_item,
                                                                      self.item_i: np.zeros(test_item.shape),
                                                                      self.item_j: np.zeros(test_item.shape)})
                    test_roc_auc = roc_auc_score(test_label, test_prob)
                    test_pr_auc = average_precision_score(test_label, test_prob)
                    print("test loss: {:.2f}, test auc: {:.4f}, test pr auc: {:.4f}".format(
                        test_loss, test_roc_auc, test_pr_auc))
                    print()












