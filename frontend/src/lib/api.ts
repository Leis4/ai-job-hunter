let tokenPromise: Promise<string> | null = null;

async function token(): Promise<string> {
  if (!tokenPromise) {
    tokenPromise = fetch("/api/session", { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => d.token as string);
  }
  return tokenPromise;
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

export async function apiPost<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const t = await token();
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-JobHunter-Token": t },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data as T;
}

export async function apiUpload<T>(path: string, file: File): Promise<T> {
  const t = await token();
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(path, { method: "POST", headers: { "X-JobHunter-Token": t }, body: form });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data as T;
}

export async function apiUploadMany<T>(path: string, files: File[]): Promise<T> {
  const t = await token();
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);
  form.append("token", t);
  const res = await fetch(path, { method: "POST", headers: { "X-JobHunter-Token": t }, body: form });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data as T;
}
