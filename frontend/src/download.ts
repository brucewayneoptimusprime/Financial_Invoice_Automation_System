// Saving an exported file. A plain link cannot carry the access token, so the file is fetched through apiFetch (which adds the
// token and opens the token dialog on a 401), turned into a Blob and saved under the server's file name. Nothing is sent anywhere.
import { ApiError } from "./api";
import { apiFetch } from "./apiBase";

// The file name from Content-Disposition (RFC 5987 filename* first), reduced to a plain name; the fallback when there is none.
export function filenameFrom(disposition: string | null, fallback: string): string {
  if (disposition) {
    const star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(disposition);
    const plain = /filename\s*=\s*"([^"]+)"/i.exec(disposition);
    let name: string | undefined;
    try {
      name = star ? decodeURIComponent(star[1].trim()) : plain?.[1];
    } catch {
      name = plain?.[1];
    }
    const safe = (name ?? "").split(/[\\/]/).pop()?.replace(/[^A-Za-z0-9._-]/g, "-");
    if (safe && safe.replace(/[-.]/g, "")) return safe;
  }
  return fallback;
}

export async function downloadFile(path: string, fallbackName: string): Promise<string> {
  const res = await apiFetch(path);
  if (!res.ok) {
    let code = "http_error";
    let message = `The export failed (the server answered ${res.status}).`;
    try {
      const body = await res.json();
      code = body.error ?? code;
      message = body.message ?? message;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, code, message);
  }
  const blob = await res.blob();
  const name = filenameFrom(res.headers.get("Content-Disposition"), fallbackName);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
  return name;
}
