"""What google-genai 3.x changed under the Gemini adapters: the default API version and the HTTP client library.

One module, so a later move to v1 or a different transport is one edit (the three adapters share it).
"""
import httpx
import httpx2

# SDK 3.x defaults the Developer API to v1 (2.x used v1beta). Our request shapes were measured on v1beta; moving to v1
# is a separate decision, so every genai.Client is built with this explicitly.
GENAI_API_VERSION = "v1beta"

# SDK 3.x builds its HTTP client on httpx2 (a fork under its own import namespace, so its exceptions are not subclasses of
# httpx's) and raises the transport errors unwrapped. The SDK still accepts an httpx client (GOOGLE_GENAI_HTTP_CLIENT),
# so both families are caught.
TIMEOUT_ERRORS = (httpx.TimeoutException, httpx2.TimeoutException)
NETWORK_ERRORS = (httpx.NetworkError, httpx2.NetworkError)
