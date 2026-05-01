import numpy as np
from sklearn import linear_model


class GeneralizedLasso:
    def __init__(self):
        self.regr = linear_model.Lasso(fit_intercept=False, tol=1e-6)
        self.betas = None
        self.err_fit = float('nan')

    # noinspection PyPep8Naming
    def fit(self, X, y, lams, w):
        w /= np.sum(w)
        sqr_w = np.sqrt(w)
        y *= sqr_w
        X = np.multiply(X, np.transpose([sqr_w]))
        n = len(y)
        p = len(lams)

        if n == 0 or np.isnan(X).any():
            self.betas = np.zeros(p)
            self.err_fit = float('nan')
        else:
            Z = np.zeros((n, p))
            for j in range(p):
                Z[:, j] = np.true_divide(X[:, j], lams[j])

            self.regr.fit(Z, y)
            gammas = self.regr.coef_
            self.betas = gammas/lams
            self.err_fit = np.sum(np.square(y - X.dot(self.betas))) + np.sum(lams.dot(np.abs(self.betas)))

    def fit_lasso(self, X, y, lams, fit_intercept=False):
        n = len(y)
        p = len(lams)

        if n == 0 or np.isnan(X).any():
            self.betas = np.zeros(p)
            self.err_fit = float('nan')
        else:
            regr = linear_model.Lasso(alpha=lams[0], fit_intercept=fit_intercept)
            regr.fit(X, y)
            gammas = regr.coef_
            self.betas = gammas
            self.err_fit = np.sum(np.square(y - X.dot(self.betas))) + np.sum(lams.dot(self.betas))

    def fit_lin_reg(self, X, y, w, fit_intercept=False):
        regr = linear_model.LinearRegression(fit_intercept=fit_intercept)

        w /= np.sum(w)
        sqr_w = np.sqrt(w)
        y *= sqr_w
        X = np.multiply(X, np.transpose([sqr_w]))

        regr.fit(X, y)
        self.betas = regr.coef_
        self.err_fit = np.sum(np.square(y - X.dot(self.betas)))
