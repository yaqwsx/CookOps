"""Disposable backend MCP mount used only by the phase-1 live smoke."""

from contextlib import asynccontextmanager

from cookops.config import Settings
from cookops.database import create_database_runtime
from cookops.mcp_resource import McpIntrospectionVerifier, create_mcp_protected_resource
from fastapi import FastAPI

settings = Settings()
runtime = create_database_runtime(str(settings.database_url))
resource = settings.mcp_resource
issuer = settings.oauth_issuer
introspection = settings.oauth_introspection_url
secret = settings.oauth_resource_server_secret
if not all(
    isinstance(value, str) and value
    for value in (resource, issuer, introspection, secret)
):
    raise RuntimeError("backend MCP smoke requires complete OAuth settings")

mcp_app = create_mcp_protected_resource(
    McpIntrospectionVerifier(
        issuer=issuer,
        resource=resource,
        introspection_url=introspection,
        resource_server_secret=secret,
    ),
    issuer=issuer,
    resource=resource,
    session_factory=runtime.session_factory,
)
other_resource = f"{resource.rsplit('/', 1)[0]}/other-mcp"
other_mcp_app = create_mcp_protected_resource(
    McpIntrospectionVerifier(
        issuer=issuer,
        resource=other_resource,
        introspection_url=introspection,
        resource_server_secret=secret,
    ),
    issuer=issuer,
    resource=other_resource,
    session_factory=runtime.session_factory,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with (
        mcp_app.router.lifespan_context(mcp_app),
        other_mcp_app.router.lifespan_context(other_mcp_app),
    ):
        yield
    await runtime.close()


app = FastAPI(title="CookOps disposable MCP", lifespan=lifespan)
app.mount("/other-mcp", other_mcp_app)
app.mount("/", mcp_app)
