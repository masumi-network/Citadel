/* The dashboard's one fetch helper.
 *
 * A port of `api()` at kb/static/app.js:567, with one deliberate behaviour
 * change. The original redirects to /login only when the boot-time
 * `GET /api/session` fails; every other 401 renders its own error text in
 * place, so a session that expires mid-visit produces a screen of
 * "Could not load ..." panels instead of a sign-in prompt. The contract map
 * calls that out as something the port should fix rather than reproduce, so a
 * 401 from any call sends the visitor to /login.
 *
 * Everything else matches: JSON in, JSON out, and a non-2xx becomes an Error
 * carrying the server's message and the status code.
 */

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** FastAPI answers a validation failure with `detail` as an array of objects. */
function flattenDetail(detail: unknown): string | null {
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((entry) =>
        entry && typeof entry === "object" && "msg" in entry
          ? String((entry as { msg: unknown }).msg)
          : null
      )
      .filter((part): part is string => Boolean(part));
    if (parts.length) return parts.join("; ");
  }
  return null;
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers ?? {}) },
  });

  if (response.status === 401) {
    // The cookie is gone or was never there. Anything else we render would be
    // an error message about a missing session, which is a sign-in prompt with
    // extra steps.
    window.location.assign("/login");
    throw new ApiError("Your session has ended. Signing you in again.", 401);
  }

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  if (!response.ok) {
    const payload = (body ?? {}) as { detail?: unknown; message?: unknown };
    const message =
      flattenDetail(payload.detail) ??
      (typeof payload.message === "string" && payload.message ? payload.message : null) ??
      "Request failed";
    throw new ApiError(message, response.status);
  }

  return body as T;
}

export function errorMessage(failure: unknown): string {
  return failure instanceof Error ? failure.message : "Request failed";
}
