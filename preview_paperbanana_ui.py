from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parent
env = Environment(
    loader=FileSystemLoader(ROOT / "app" / "templates"),
    autoescape=select_autoescape(["html", "xml"]),
)


def user(role: str = "user") -> SimpleNamespace:
    return SimpleNamespace(
        username="B308" if role == "admin" else "s2a_1",
        role=role,
        gen_quota_remaining=12,
        can_optimize_method=True,
        can_edit_image=True,
    )


common = {
    "title": "PaperFigure Preview",
    "hide_account_features": False,
    "generation_unit_price": 1.0,
    "error": "",
    "success": "",
    "pending_job_finish_url": "",
    "showcase_cards": [
        {
            "title": "Framework Diagram",
            "desc": "SCI-style method overview",
            "image_url": "/assets/scenario_architecture.png",
        },
        {
            "title": "Dataset Figure",
            "desc": "Clean benchmark visual",
            "image_url": "/assets/dataset-on-hf-xl.png",
        },
        {
            "title": "Architecture Variant",
            "desc": "Publication-ready layout",
            "image_url": "/assets/scenario_architecture1.png",
        },
    ],
    "example_method_options": ["PaperVizAgent Framework", "Retriever-Planner-Critic"],
    "example_caption_options": ["Overview figure", "Ablation plot"],
    "example_methods_json": "{}",
    "example_captions_json": "{}",
    "available_modes": [
        {"id": "demo_planner_critic", "label": "Planner + Critic"},
        {"id": "demo_full", "label": "Full Agent Pipeline"},
    ],
    "selected_mode": "demo_planner_critic",
    "gen_estimated_seconds": 210,
    "gen_estimated_label": "约 3 分 30 秒",
    "gen_last_actual_seconds": None,
    "gen_last_actual_label": "",
    "gallery_items": [
        {"id": "preview_figure_01", "ts": "2026-04-28 16:20"},
        {"id": "preview_figure_02", "ts": "2026-04-28 16:24"},
    ],
    "results": [
        {"idx": 0, "url": "/assets/scenario_architecture.png", "title": "Candidate 0"},
        {"idx": 1, "url": "/assets/scenario_architecture1.png", "title": "Candidate 1"},
    ],
    "result_reference_images": [],
    "price_packages": [
        SimpleNamespace(id="preview_10", label="10 次体验包", times=10, price_yuan=10),
        SimpleNamespace(id="preview_30", label="30 次标准包", times=30, price_yuan=24),
    ],
    "recent_orders": [],
    "total_yuan": 10,
    "pwd_msg": "",
    "user_message_rows": [],
    "alipay_qr_image_url": "",
    "wechat_qr_image_url": "",
    "customer_service_wechat_qr_image_url": "",
}


def render(name: str, **context: object) -> bytes:
    payload = dict(common)
    payload.update(context)
    return env.get_template(name).render(**payload).encode("utf-8")


class Handler(SimpleHTTPRequestHandler):
    def send_html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = unquote(self.path.split("?", 1)[0])
        if path in {"/", "/login-preview"}:
            return self.send_html(render("login.html", show_local_login=True, user=None))
        if path == "/index-preview":
            return self.send_html(render("index.html", user=user("user")))
        if path == "/admin-preview":
            return self.send_html(
                render(
                    "admin_users_accounts.html",
                    user=user("admin"),
                    users={"s2a_1": user("user"), "B308": user("admin")},
                    view="users",
                    q="",
                    role_filter="",
                    auth_filter="",
                    page=1,
                    page_size=20,
                    total_pages=1,
                    total_filtered=2,
                    overview_stats={},
                    detail_user=None,
                    detail_info=None,
                    message_rows=[],
                    message_stats={},
                    admin_orders=[],
                    order_user="",
                    order_status="",
                    order_page=1,
                    order_page_size=20,
                    order_total=0,
                    order_total_pages=1,
                    tier_order=[],
                    tier_templates=[],
                    tier_override_ids=[],
                    default_new_user_mode="",
                    user_crud_msg="",
                    tier_label=lambda value: value,
                )
            )
        if path.startswith("/static/"):
            self.directory = str(ROOT / "app")
            self.path = path
            return super().do_GET()
        if path.startswith("/assets/"):
            self.directory = str(ROOT)
            self.path = path
            return super().do_GET()
        return self.send_error(404)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8098), Handler)
    print("Preview: http://127.0.0.1:8098/login-preview", flush=True)
    server.serve_forever()
