"""ML-based part cost estimation, replacing the flat formula in
CadAdapter.estimate_cost() with a Random Forest Regression model trained on
synthetic order data that is grounded in materials.csv's real cost_per_kg
figures.

Modeled on the approach in Nirmalakumari K. et al., "Efficient Method for
Product Cost Estimation using Artificial Intelligence Techniques," 2025 3rd
ICAECA, DOI: 10.1109/ICAECA63854.2025.11012624 -- geometric features
(volume, surface area, face count, material density/mass) plus order-level
features (material type, order quantity, order year, supplier), Random
Forest Regression tuned with Grid Search, 75/25 train/test split, MSE.
"""
