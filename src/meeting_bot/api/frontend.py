"""Built frontend for the explicit HTTPS test launcher; no preview identities."""
from pathlib import Path
from starlette.staticfiles import StaticFiles

CSP=("default-src 'none'; script-src 'self' https://telegram.org; "
     "style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; "
     "base-uri 'none'; object-src 'none'; form-action 'none'; "
     "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org")


class FrontendFiles(StaticFiles):
    async def get_response(self,path,scope):
        response=await super().get_response(path,scope)
        response.headers["Content-Security-Policy"]=CSP
        response.headers["Cache-Control"]="no-store"
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Referrer-Policy"]="no-referrer"
        return response


def mount_frontend(app,assets):
    assets=Path(assets)
    if not (assets/"index.html").is_file() or assets.is_symlink():
        raise ValueError("Build the Mini App before using --frontend")
    # Serve only the build directory; reject redirections and accidental secret files.
    for item in assets.rglob("*"):
        if item.is_symlink() or item.name.startswith(".") or (item.is_file() and item.suffix.lower() not in {".html",".js",".css",".svg",".png",".jpg",".jpeg",".webp",".ico",".woff",".woff2"}):
            raise ValueError("Unexpected file in frontend build")
    app.mount("/app",FrontendFiles(directory=assets,html=True,follow_symlink=False),name="mini-app-frontend")
