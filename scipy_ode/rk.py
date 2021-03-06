from __future__ import division, print_function, absolute_import

import numpy as np
from scipy.interpolate import PPoly

from .solver import OdeSolver, SolverStatus
from .common import select_initial_step, norm, PointSpline, validate_rtol, validate_atol

# Multiply steps computed from asymptotic behaviour of errors by this.
SAFETY = 0.9

MAX_FACTOR = 5  # Maximum allowed increase in a step size.
MIN_FACTOR = 0.2  # Minimum allowed decrease in a step size.


class RungeKutta(OdeSolver):
    """Abstract base class for Runge-Kutta solvers

    This abstract base class consolidates all the functionality of the Runge-Kutta
    solvers. Built-in subclasses are ``RungeKutta23`` and ``RungaKutta45``.

    Notation for Butcher tableau is as in [1]_.

    Parameters
    ----------
    fun : callable, (t, y) -> ydot
        The ODE system
    y0 : array_like, shape (n,)
        Initial values for ``y``
    t0 : float
        The initial value of ``t``
    t_crit : float
        The boundary of the ODE system.
    C : ndarray, shape (n_stages - 1,)
        Coefficients for incrementing x for computing RK stages. The value for
        the first stage is always zero, thus it is not stored.
    A : list of ndarray, length n_stages - 1
        Coefficients for combining previous RK stages for computing the next
        stage. For explicit methods the coefficients above the main diagonal
        are zeros, so they are stored as a list of arrays of increasing
        lengths. The first stage is always just `f`, thus no coefficients are
        required.
    B : ndarray, shape (n_stages,)
        Coefficients for combining RK stages for computing the final
        prediction.
    E : ndarray, shape (n_stages + 1,)
        Coefficients for estimating the error of a less accurate method. They
        are computed as the difference between b's in an extended tableau.
    M : ndarray, shape (n_stages + 1,), or None
        Coefficients to compute y(x + 0.5 * h) from RK stages with a 4-rd order
        accuracy. Then it can be used for quartic interpolation with a 4-rd order
        accuracy.
    order : int
        Order of local truncation error
        # TODO: can this determined automatically from the other parameters
    step_size : float or None
        The initial step size
    max_step : float
        The maximum step size permitted
    rtol : float
        Relative tolerance
    atol: float or array, shape (n,1)
        Absolute tolerance

    Attributes
    ----------
    f : array, shape (n,)
        A convenience property that gets ``state.f``

    References
    ----------
    .. [1] E. Hairer, S. P. Norsett G. Wanner, "Solving Ordinary Differential
           Equations I: Nonstiff Problems", Sec. II.4.
    """
    class OdeState(OdeSolver.OdeState):
        def __init__(self, t, y, f, ym=None):
            super().__init__(t, y)
            self.f = f
            self.ym = ym

    def __init__(self, fun, y0, t0, t_crit, C, A, B, E, M, order, step_size=None, max_step=np.inf, rtol=1e-3,
                 atol=1e-6):
        fun, y0, t0, t_crit = self.check_arguments(fun, y0, t0, t_crit)
        f0 = fun(t0, y0)

        state = self.OdeState(t0, y0, f0)
        super().__init__(fun, state, t_crit)

        self.C = C
        self.A = A
        self.B = B
        self.E = E
        self.M = M
        self.K = np.empty((B.size + 1, self.n))
        self.order = order

        self.max_step = max_step
        self.rtol = validate_rtol(rtol)
        self.atol = validate_atol(atol, self.n)

        if step_size is None:
            step_size = select_initial_step(self.fun, self.t, t_crit, self.y, self.f, order, rtol, atol)
        self.step_size = min(step_size, max_step)

    @property
    def f(self):
        return self.state.f

    def step(self):
        if self.status != SolverStatus.running and self.status != SolverStatus.started:
            # Only take a step is the solver is running
            return

        t = self.t
        y = self.y
        h_abs = self.step_size
        s = self.direction
        b = self.t_crit
        atol = self.atol
        rtol = self.rtol
        fun = self.fun

        f = self.f
        d = abs(b - t)

        # Loop until an appropriately small step is taken
        while True:
            if h_abs > d:
                h_abs = d
                t_new = b
                h = h_abs * s
            else:
                h = h_abs * s
                t_new = t + h

            y_new, f_new, error = rk_step(fun, t, y, f, h, self.A, self.B, self.C, self.E, self.K)
            scale = atol + np.maximum(np.abs(y), np.abs(y_new)) * rtol
            error_norm = norm(error / scale)

            if error_norm > 1:
                h_abs *= max(MIN_FACTOR, SAFETY * error_norm ** (-1 / self.order))
                continue
            else:
                break

        with np.errstate(divide='ignore'):
            h_abs *= min(MAX_FACTOR, max(1, SAFETY * error_norm**(-1/self.order)))
        h_abs = min(h_abs, self.max_step)

        if self.M is not None:
            ym = y + 0.5 * h * np.dot(self.K.T, self.M)
        else:
            ym = None

        self.state = self.OdeState(t_new, y_new, fun(t_new, y_new), ym)

        self.step_size = h_abs

        if t_new == b:
            self.status = SolverStatus.finished
        elif t_new == t:  # h less than spacing between numbers.
            self.status = SolverStatus.failed
        else:
            self.status = SolverStatus.running

    def spline(self, states):
        if len(states) == 1:
            state = states[0]
            return PointSpline(state.t, state.y)

        t = np.asarray([state.t for state in states])
        y = np.asarray([state.y for state in states])
        f = np.asarray([state.f for state in states])
        if self.M is not None:
            ym = np.asarray([state.ym for state in states[1:]])  # No ym on first point
        else:
            ym = None

        if t[-1] < t[0]:
            t = t[::-1]
            y = y[::-1]
            if ym is not None:
                ym = ym[::-1]
            f = f[::-1]

        h = np.diff(t)

        y0 = y[:-1]
        y1 = y[1:]
        f0 = f[:-1]
        f1 = f[1:]

        n_points, n = y.shape
        h = h[:, None]
        if ym is None:
            c = np.empty((4, n_points - 1, n))
            slope = (y1 - y0) / h
            tt = (f0 + f1 - 2 * slope) / h
            c[0] = tt / h
            c[1] = (slope - f0) / h - tt
            c[2] = f0
            c[3] = y0
        else:
            c = np.empty((5, n_points - 1, n))
            c[0] = (-8 * y0 - 8 * y1 + 16 * ym) / h ** 4 + (- 2 * f0 + 2 * f1) / h ** 3
            c[1] = (18 * y0 + 14 * y1 - 32 * ym) / h ** 3 + (5 * f0 - 3 * f1) / h ** 2
            c[2] = (-11 * y0 - 5 * y1 + 16 * ym) / h ** 2 + (-4 * f0 + f1) / h
            c[3] = f0
            c[4] = y0

        c = np.rollaxis(c, 2)
        return PPoly(c, t, extrapolate=False, axis=1)


def rk_step(fun, t, y, f, h, A, B, C, E, K):
    """Perform a single Runge-Kutta step.

    This function computes a prediction of an explicit Runge-Kutta method and
    also estimates the error of a less accurate method.

    Notation for Butcher tableau is as in [1]_.

    Parameters
    ----------
    fun : callable
        Right-hand side of the system.
    t : float
        Current value of the independent variable.
    y : ndarray, shape (n,)
        Current value of the solution.
    f : ndarray, shape (n,)
        Current value of the derivative of the solution, i.e. ``fun(x, y)``.
    h : float, shape (n,)
        Step for x to use.
    A : list of ndarray, length n_stages - 1
        Coefficients for combining previous RK stages for computing the next
        stage. For explicit methods the coefficients above the main diagonal
        are zeros, so they are stored as a list of arrays of increasing
        lengths. The first stage is always just `f`, thus no coefficients are
        required.
    B : ndarray, shape (n_stages,)
        Coefficients for combining RK stages for computing the final
        prediction.
    C : ndarray, shape (n_stages - 1,)
        Coefficients for incrementing x for computing RK stages. The value for
        the first stage is always zero, thus it is not stored.
    E : ndarray, shape (n_stages + 1,)
        Coefficients for estimating the error of a less accurate method. They
        are computed as the difference between b's in an extended tableau.
    K : ndarray, shape (n_stages + 1, n)
        Storage array for putting RK stages here. Stages are stored in rows.

    Returns
    -------
    y_new : ndarray, shape (n,)
        Solution at x + h computed with a higher accuracy.
    f_new : ndarray, shape (n,)
        Derivative ``fun(x + h, y_new)``.
    error : ndarray, shape (n,)
        Error estimate.

    References
    ----------
    .. [1] E. Hairer, S. P. Norsett G. Wanner, "Solving Ordinary Differential
           Equations I: Nonstiff Problems", Sec. II.4.
    """
    K[0] = f
    for s, (a, c) in enumerate(zip(A, C)):
        dy = np.dot(K[:s + 1].T, a) * h
        K[s + 1] = fun(t + c * h, y + dy)

    y_new = y + h * np.dot(K[:-1].T, B)
    f_new = fun(t + h, y_new)

    K[-1] = f_new
    error = np.dot(K.T, E) * h

    return y_new, f_new, error


class RungeKutta23(RungeKutta):
    """Bogacki-Shampine Runge-Kutta ODE solver

    An explicit Runge-Kutta method of order 3 with an automatic
    step size control [1]_. A 3-th order accurate cubic Hermit
    polynomial is used for the continuous extension.

    Parameters
    ----------
    fun : callable, (t, y) -> ydot
        The ODE system
    y0 : array_like, shape (n,)
        Initial values for ``y``
    t0 : float
        The initial value of ``t``
    t_crit : float
        The boundary of the ODE system.
    step_size : float or None
        The initial step size
    max_step : float
        The maximum step size permitted
    rtol : float
        Relative tolerance
    atol: float or array, shape (n,1)
        Absolute tolerance

    References
    ----------
    .. [1] P. Bogacki, L.F. Shampine, "A 3(2) Pair of Runge-Kutta Formulas",
           Appl. Math. Lett. Vol. 2, No. 4. pp. 321-325, 1989.
    """
    def __init__(self, fun, y0, t0=0, t_crit=np.inf, *, step_size=None, max_step=np.inf, rtol=1e-3, atol=1e-6, **_):
        # Bogacki–Shampine scheme.
        C23 = np.array([1 / 2, 3 / 4])
        A23 = [np.array([1 / 2]),
               np.array([0, 3 / 4])]
        B23 = np.array([2 / 9, 1 / 3, 4 / 9])
        # Coefficients for estimation errors. The difference between B's for lower
        # and higher order accuracy methods.
        E23 = np.array([5 / 72, -1 / 12, -1 / 9, 1 / 8])

        order = 3

        super().__init__(fun, y0, t0, t_crit, C23, A23, B23, E23, None, order, step_size, max_step, rtol, atol)


class RungeKutta45(RungeKutta):
    """Dormand-Prince Runge-Kutta ODE solver

    An explicit Runge-Kutta method of order 5 with an
    automatic step size control [1]_. A 4-th order accurate quartic
    polynomial is used for the continuous extension [2]_. This is the
    default ODE solver in scipy.

    Parameters
    ----------
    fun : callable, (t, y) -> ydot
        The ODE system
    y0 : array_like, shape (n,)
        Initial values for ``y``
    t0 : float
        The initial value of ``t``
    t_crit : float
        The boundary of the ODE system.
    step_size : float or None
        The initial step size
    max_step : float
        The maximum step size permitted
    rtol : float
        Relative tolerance
    atol: float or array, shape (n,1)
        Absolute tolerance

    References
    ----------
    .. [1] J. R. Dormand, P. J. Prince, "A family of embedded Runge-Kutta
           formulae", Journal of Computational and Applied Mathematics, Vol. 6,
           No. 1, pp. 19-26, 1980.
    .. [2] L. W. Shampine, "Some Practical Runge-Kutta Formulas", Mathematics
           of Computation,, Vol. 46, No. 173, pp. 135-150, 1986.
    """

    def __init__(self, fun, y0, t0=0, t_crit=np.inf, *, step_size=None, max_step=np.inf, rtol=1e-3, atol=1e-6, **_):
        # Dormand–Prince scheme.
        C45 = np.array([1 / 5, 3 / 10, 4 / 5, 8 / 9, 1])
        A45 = [np.array([1 / 5]),
               np.array([3 / 40, 9 / 40]),
               np.array([44 / 45, -56 / 15, 32 / 9]),
               np.array([19372 / 6561, -25360 / 2187, 64448 / 6561, -212 / 729]),
               np.array([9017 / 3168, -355 / 33, 46732 / 5247, 49 / 176, -5103 / 18656])]
        B45 = np.array([35 / 384, 0, 500 / 1113, 125 / 192, -2187 / 6784, 11 / 84])
        E45 = np.array([-71 / 57600, 0, 71 / 16695, -71 / 1920, 17253 / 339200, -22 / 525, 1 / 40])

        # Coefficients to compute y(x + 0.5 * h) from RK stages with a 4-rd order
        # accuracy. Then it can be used for quartic interpolation with a 4-rd order
        # accuracy.
        M45 = np.array([613 / 3072, 0, 125 / 159, -125 / 1536, 8019 / 54272, -11 / 96, 1 / 16])

        order = 5

        super().__init__(fun, y0, t0, t_crit, C45, A45, B45, E45, M45, order, step_size, max_step, rtol, atol)
