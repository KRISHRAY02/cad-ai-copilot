"""Trains a Random Forest Regression model to predict part cost, following
the approach in Nirmalakumari K. et al., "Efficient Method for Product Cost
Estimation using Artificial Intelligence Techniques," 2025 3rd ICAECA,
DOI: 10.1109/ICAECA63854.2025.11012624:

- Features: geometric (volume, surface area, face count, bend count,
  material density, mass) + order-level (material type, order quantity,
  order year, supplier, machine type, manufacturing_process -- CNC
  Machining / Injection Molding / Sheet Metal, see production_cost.py).
- Model: RandomForestRegressor, hyperparameters tuned with GridSearchCV
  (n_estimators, max_depth, min_samples_split).
- Split: 75% train / 25% test, matching the paper.
- Evaluation: mean squared error (MSE) on the held-out test set, plus an
  average accuracy percentage derived from it the same way the paper
  reports results (see `_mse_to_accuracy_percent` below).

Trains on cost_model/synthetic_cost_dataset.csv (generate it first with
generate_synthetic_dataset.py). Saves the fitted model + fitted encoders
together to cost_model/trained_model.joblib via joblib, so predict.py can
load one file and reproduce the exact same feature encoding used here.

Run with:

    C:\\Python314\\python.exe cost_model/generate_synthetic_dataset.py
    C:\\Python314\\python.exe cost_model/train_model.py
"""

import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_squared_error
from sklearn.model_selection import GridSearchCV, train_test_split

# Lets this file be run directly (`python cost_model/train_model.py`) as
# well as imported as part of the `cost_model` package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cost_model.features import FEATURE_COLUMNS, encode_categoricals, fit_encoders  # noqa: E402

_DATASET_CSV_PATH = Path(__file__).parent / "synthetic_cost_dataset.csv"
_MODEL_PATH = Path(__file__).parent / "trained_model.joblib"

_TARGET_COLUMN = "cost_inr"

# Grid Search tries every combination of these values (via k-fold cross-
# validation on the training set) and keeps whichever combination gives
# the lowest cross-validated error -- this is what "tuning" means below,
# as opposed to picking these numbers by hand.
_PARAM_GRID = {
    "n_estimators": [100, 200, 300],
    "max_depth": [None, 10, 20, 30],
    "min_samples_split": [2, 5, 10],
}

_TEST_SIZE = 0.25  # 75/25 train/test split, matching the paper.
_RANDOM_SEED = 42


def _mse_to_accuracy_percent(mse: float, y_true: np.ndarray) -> float:
    """Convert MSE into an average accuracy percentage, the way the paper
    reports its headline result.

    MSE is in squared-INR, which isn't directly interpretable as
    "accuracy". Take sqrt(MSE) to get RMSE back into INR (the model's
    typical prediction error), then express that error as a percentage of
    the average actual cost and subtract from 100 -- i.e. "on average,
    predictions are within (100 - this)% of the true cost". This is a
    simple, explainable derivation suitable for a report's Methodology
    section, not a formal accuracy metric like R^2.
    """
    rmse = np.sqrt(mse)
    mean_actual = float(np.mean(y_true))
    error_percent = (rmse / mean_actual) * 100
    return max(0.0, 100.0 - error_percent)


def train() -> dict:
    if not _DATASET_CSV_PATH.exists():
        raise FileNotFoundError(
            f"{_DATASET_CSV_PATH} not found. Run "
            f"generate_synthetic_dataset.py first."
        )

    df = pd.read_csv(_DATASET_CSV_PATH)
    X_raw = df[FEATURE_COLUMNS]
    y = df[_TARGET_COLUMN].to_numpy()

    encoders = fit_encoders(X_raw)
    X_encoded = encode_categoricals(X_raw, encoders)

    # 75% train / 25% test split, matching the paper. random_state fixes
    # which rows land in which split, so results are reproducible.
    X_train, X_test, y_train, y_test = train_test_split(
        X_encoded, y, test_size=_TEST_SIZE, random_state=_RANDOM_SEED
    )

    # GridSearchCV exhaustively trains a RandomForestRegressor once per
    # combination of _PARAM_GRID's values (3 x 4 x 3 = 36 combinations),
    # each evaluated with 5-fold cross-validation on the training set only
    # (the test set stays untouched until final evaluation below), and
    # keeps the combination with the best average cross-validated score.
    # This is the "tuned with Grid Search" step from the paper --
    # n_estimators (how many trees), max_depth (how deep each tree can
    # grow, controlling overfitting), and min_samples_split (minimum
    # samples required to split a node, also controlling overfitting).
    grid_search = GridSearchCV(
        estimator=RandomForestRegressor(random_state=_RANDOM_SEED),
        param_grid=_PARAM_GRID,
        cv=5,
        scoring="neg_mean_squared_error",
        n_jobs=-1,
    )
    grid_search.fit(X_train, y_train)

    best_model = grid_search.best_estimator_

    y_pred = best_model.predict(X_test)
    mse = mean_squared_error(y_test, y_pred)
    accuracy_percent = _mse_to_accuracy_percent(mse, y_test)

    joblib.dump(
        {
            "model": best_model,
            "encoders": encoders,
            "feature_columns": FEATURE_COLUMNS,
        },
        _MODEL_PATH,
    )

    return {
        "best_params": grid_search.best_params_,
        "test_mse": mse,
        "test_rmse_inr": float(np.sqrt(mse)),
        "accuracy_percent": accuracy_percent,
        "train_rows": len(X_train),
        "test_rows": len(X_test),
        "model_path": str(_MODEL_PATH),
    }


def main() -> None:
    results = train()
    print("Random Forest cost model -- training results")
    print("=" * 50)
    print(f"Train rows: {results['train_rows']}  |  Test rows: {results['test_rows']}")
    print(f"Best Grid Search params: {results['best_params']}")
    print(f"Test MSE: {results['test_mse']:.2f} (INR^2)")
    print(f"Test RMSE: {results['test_rmse_inr']:.2f} INR")
    print(f"Derived average accuracy: {results['accuracy_percent']:.2f}%")
    print(f"Saved model + encoders -> {results['model_path']}")


if __name__ == "__main__":
    main()
