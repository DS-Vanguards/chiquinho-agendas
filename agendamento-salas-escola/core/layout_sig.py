import base64
import hashlib
import json

from flask import Response, url_for

from config import _LS_BRAND, _LS_MARK, _LS_REF, _LS_RIGHTS, _LS_YEAR


def _ref_url() -> str:
    return base64.b64decode(_LS_REF).decode("utf-8")


def layout_token() -> str:
    raw = f"{_ref_url()}|{_LS_BRAND}|{_LS_YEAR}|{_LS_RIGHTS}|{_LS_MARK}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _marker_ok(html: str) -> bool:
    url = _ref_url()
    return (
        f'data-ls="{_LS_MARK}"' in html
        and url in html
        and _LS_BRAND in html
        and _LS_RIGHTS in html
    )


def _overlay_fragment(script_url: str) -> str:
    payload = json.dumps(
        {
            "m": "Uma alteração na marca d'água deste site foi detectado, contate um membro da",
            "b": _LS_BRAND,
            "u": _ref_url(),
            "t": "sobre o ocorrido",
        },
        ensure_ascii=False,
    )
    token = layout_token()
    url = _ref_url()
    inner = (
        "Uma alteração na marca d'água deste site foi detectado, "
        f'contate um membro da <a href="{url}" target="_blank" rel="noopener">{_LS_BRAND}</a> '
        "sobre o ocorrido."
    )
    return (
        f'<div id="_lsx" class="lsx-o" hidden role="alert">'
        f'<div class="lsx-i"><p>{inner}</p></div>'
        f"</div>"
        f'<script id="_lsg" type="application/json">{payload}</script>'
        f'<script src="{script_url}?v={token}" defer></script>'
        f'<script>(function(){{var s=document.createElement("script");'
        f's.src="{script_url}?v={token}";s.defer=true;'
        f'document.body.appendChild(s)}})();</script>'
    )


def _guard_script_only(script_url: str) -> str:
    token = layout_token()
    return (
        f'<script src="{script_url}?v={token}" defer></script>'
        f'<script>(function(){{var s=document.createElement("script");'
        f's.src="{script_url}?v={token}";s.defer=true;'
        f'document.body.appendChild(s)}})();</script>'
    )


def verify_rendered_html(html: str) -> bool:
    if not html or "<html" not in html.lower():
        return True
    return _marker_ok(html)


def bind(app) -> None:
    @app.context_processor
    def _layout_ctx():
        return {"_lt": layout_token()}

    @app.after_request
    def _finalize_layout(response: Response):
        ctype = response.content_type or ""
        if "text/html" not in ctype:
            return response
        if response.status_code >= 400:
            return response
        try:
            body = response.get_data(as_text=True)
        except Exception:
            return response
        if "</body>" not in body:
            return response

        needs_overlay = not _marker_ok(body)
        has_guard = "ui-core.js" not in body
        script_url = url_for("static", filename="js/ui-core.js")

        if needs_overlay:
            body = body.replace("</body>", _overlay_fragment(script_url) + "</body>", 1)
        elif has_guard:
            body = body.replace("</body>", _guard_script_only(script_url) + "</body>", 1)

        if needs_overlay or has_guard:
            response.set_data(body)
            response.headers["X-Layout-Sig"] = layout_token()[:8]
        return response
