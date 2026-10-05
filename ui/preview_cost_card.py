"""Standalone preview harness for ui/components/cost_card.py.

Renders the redesigned cost breakdown card against real estimate_cost()
output (via CAD_ADAPTER=mock -- synthetic data, no CAD software needed)
for all three manufacturing processes, plus the specific edge-case
variants called out in the review request: a very small material cost,
a missing ML estimate, a long material name, dark theme, and a narrow
window. A theme switch and a width toggle at the top let you flip
through those live instead of needing six separate screenshots.

Run with:  C:\\Python314\\python.exe ui/preview_cost_card.py
Opens in a browser tab (ft.AppView.WEB_BROWSER) rather than a native
window specifically so it can be screenshotted/inspected like any other
web page.
"""

import copy
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["CAD_ADAPTER"] = "mock"

import flet as ft

import mcp_server
from ui.components.cost_card import build_cost_card

NARROW_WIDTH = 420
NORMAL_WIDTH = 760


def _fetch_real_sample(process: str, quantity: int = 19) -> dict:
    """A real estimate_cost() result against MockAdapter's synthetic
    part -- not hand-written, so these sample numbers are guaranteed to
    be in the exact shape the live app actually produces.
    """
    return mcp_server.estimate_cost(manufacturing_process=process, quantity=quantity)


def _build_samples() -> dict[str, dict]:
    cnc = _fetch_real_sample("CNC Machining", quantity=19)
    injection_molding = _fetch_real_sample("Injection Molding", quantity=250)
    sheet_metal = _fetch_real_sample("Sheet Metal", quantity=50)

    tiny_cost = copy.deepcopy(cnc)
    tiny_cost["material_cost_per_unit_inr"] = 0.03
    tiny_cost["production_cost_per_unit_inr"] = 0.08
    tiny_cost["total_cost_per_unit_inr"] = 0.11
    tiny_cost["total_cost_for_quantity_inr"] = round(0.11 * tiny_cost["quantity"], 2)
    tiny_cost["ml_predicted_total_cost_per_unit_inr"] = 0.42
    tiny_cost["production_cost_breakdown"]["setup"]["estimated_cost_inr"] = 0.05
    tiny_cost["production_cost_breakdown"]["other_features"]["estimated_cost_inr"] = 0.03

    missing_ml = copy.deepcopy(injection_molding)
    missing_ml.pop("ml_predicted_total_cost_per_unit_inr", None)

    long_name = copy.deepcopy(sheet_metal)
    long_name["material_used"] = (
        "AISI 316L Stainless Steel, Cold-Rolled, Annealed, Food-Grade Certified Sheet"
    )

    return {
        "CNC Machining (real mock data)": cnc,
        "Injection Molding (real mock data)": injection_molding,
        "Sheet Metal (real mock data)": sheet_metal,
        "Edge case: very small cost": tiny_cost,
        "Edge case: missing ML estimate": missing_ml,
        "Edge case: long material name": long_name,
    }


def main(page: ft.Page) -> None:
    page.title = "Cost Card Preview"
    page.scroll = ft.ScrollMode.AUTO
    page.bgcolor = "#F5F7FA"
    page.padding = 24

    samples = _build_samples()
    state = {"dark": False, "width": NORMAL_WIDTH}
    cards_column = ft.Column(spacing=24)

    def render() -> None:
        page.bgcolor = "#141822" if state["dark"] else "#F5F7FA"
        cards_column.controls.clear()
        for title, data in samples.items():
            label = ft.Text(
                f"{title}  —  width {state['width']}px, {'dark' if state['dark'] else 'light'} theme",
                size=13,
                weight=ft.FontWeight.W_600,
                color="#C7D1DE" if state["dark"] else "#4A5568",
            )
            card, _animate_in = build_cost_card(
                data,
                dark=state["dark"],
                width_hint=state["width"],
                on_what_if=lambda e: None,
            )
            card.opacity = 1
            card.offset = ft.Offset(0, 0)
            wrapper = ft.Container(content=card, width=state["width"])
            cards_column.controls.append(ft.Column([label, wrapper], spacing=8))
        page.update()

    def toggle_theme(e: ft.ControlEvent) -> None:
        state["dark"] = not state["dark"]
        theme_switch_label.value = "Dark theme: ON" if state["dark"] else "Dark theme: OFF"
        render()

    def toggle_width(e: ft.ControlEvent) -> None:
        state["width"] = NARROW_WIDTH if state["width"] == NORMAL_WIDTH else NORMAL_WIDTH
        width_button_label.value = f"Window width: {state['width']}px (click to toggle)"
        render()

    theme_switch_label = ft.Text("Dark theme: OFF", size=13, weight=ft.FontWeight.W_600)
    width_button_label = ft.Text(f"Window width: {state['width']}px (click to toggle)", size=13, weight=ft.FontWeight.W_600)

    controls_bar = ft.Row(
        [
            ft.Container(
                content=theme_switch_label,
                on_click=toggle_theme,
                ink=True,
                bgcolor="#2F6FED",
                padding=ft.Padding(12, 8, 12, 8),
                border_radius=8,
            ),
            ft.Container(
                content=width_button_label,
                on_click=toggle_width,
                ink=True,
                bgcolor="#2F6FED",
                padding=ft.Padding(12, 8, 12, 8),
                border_radius=8,
            ),
        ],
        spacing=12,
    )
    for c in controls_bar.controls:
        c.content.color = "#FFFFFF"

    page.add(controls_bar, cards_column)
    render()


if __name__ == "__main__":
    ft.app(target=main, view=ft.AppView.WEB_BROWSER, port=8552)
