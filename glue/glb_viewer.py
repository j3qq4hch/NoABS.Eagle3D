"""
glb_viewer.py
-------------
Минимальный тест 3D просмотра.
Кладёт model.glb в ту же папку и запускает окно просмотра.
"""
import base64
import webview
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent


class ViewerAPI:
    def get_model_glb(self):
        p = TOOL_DIR / "model.glb"
        if not p.exists():
            return {"error": f"model.glb не найден: {p}"}
        data = p.read_bytes()
        return {"glb": base64.b64encode(data).decode(), "size": len(data)}


api = ViewerAPI()

window = webview.create_window(
    "GLB Viewer Test",
    url=str(TOOL_DIR / "ui" / "viewer_test.html"),
    js_api=api,
    width=900, height=700,
)
webview.start(debug=True)