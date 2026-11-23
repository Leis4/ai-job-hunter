import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle, Check, Loader2, Pause, RefreshCw, Sparkles,
} from "lucide-react";
import { apiGet, apiPost } from "@/lib/api";
import { PageHeader, ErrorState, LoadingState } from "@/components/PageChrome";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input, Select, Textarea } from "@/components/ui/input";

const MODES = [["observer", "Только поиск", "Находит и готовит, ничего не отправляет"], ["assisted", "С подтверждением", "Каждую отправку подтверждаете вы"], ["autopilot", "Автопилот", "Отправляет только по вашим правилам"]] as const;

export default function AutopilotPage() {
  const [data, setData] = useState<any>(null);
  const [decisions, setDecisions] = useState<any[]>([]);
  const [actions, setActions] = useState<any[]>([]);
  const [policy, setPolicy] = useState<any>({});
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [manage, setManage] = useState(false);
  const [browser, setBrowser] = useState<any>(null);
  const [tech, setTech] = useState(false);
  const [answers, setAnswers] = useState<Record<number, string>>({});

  async function load() {
    try {
      const [d, q, a] = await Promise.all([
        apiGet<any>("/api/autopilot"), apiGet<any>("/api/autopilot/decisions"), apiGet<any>("/api/autopilot/actions"),
      ]);
      setData(d); setPolicy(d.policy || {}); setDecisions(q.items || []); setActions(a.items || []); setError("");
    } catch (e) { setError((e as Error).message); }
  }
  useEffect(() => { load(); }, []);

  async function run(label: string, fn: () => Promise<any>) {
    setBusy(label); setError(""); setNotice("");
    try { const r = await fn(); setNotice(typeof r === "string" ? r : "Готово."); await load(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  async function savePolicy() {
    await run("policy", async () => {
      const r = await apiPost<any>("/api/autopilot/policy", { policy });
      if (!r.ok) throw new Error(r.reason);
      return "Настройки сохранены. Автопилот не включается автоматически.";
    });
  }
  async function stop() {
    await run("stop", async () => { await apiPost("/api/autopilot/stop", {}); setPolicy((p: any) => ({ ...p, enabled: false, auto_apply: false, auto_replies: false })); return "Автопилот остановлен."; });
  }
  async function browserLogin() {
    await run("browser-login", async () => {
      const r = await apiPost<any>("/api/hh/browser/login", { timeout_minutes: 8 });
      setBrowser(r);
      if (!r.ok) throw new Error(r.reason || "Вход не завершён");
      return "hh.ru подключён через браузер.";
    });
  }
  async function browserCheck() {
    await run("browser-check", async () => { const r = await apiPost<any>("/api/hh/browser/check", {}); setBrowser(r); return r.logged_in ? "Сессия активна." : (r.reason || "Нужно войти заново."); });
  }
  async function browserHistory() {
    await run("browser-history", async () => { const r = await apiPost<any>("/api/hh/browser/capture-history", { pages: 5 }); if (!r.ok) throw new Error(r.reason); return `Сохранено страниц: ${r.pages}. Импортируйте их в Статистике.`; });
  }
  async function decide(id: number, status: string) {
    await run(`decision-${id}`, async () => { await apiPost("/api/autopilot/decision", { id, status, answer: answers[id] || "" }); return status === "approved" ? "Подтверждено." : "Отложено."; });
  }
  function toggle(key: string) {
    const next = !policy[key];
    if (next && key === "auto_apply" && !window.confirm("Включить автоматические отклики? Отклики будут отправляться без вашего подтверждения в рамках лимитов.")) return;
    if (next && key === "auto_replies" && !window.confirm("Включить автоматические ответы работодателям? Сообщения будут отправляться без подтверждения.")) return;
    setPolicy({ ...policy, [key]: next });
  }

  if (error && !data) return <ErrorState error={error} onRetry={load} />;
  if (!data) return <LoadingState rows={4} />;
  const s = data.stats || {};
  const connected = !!browser?.logged_in;
  const profileExists = data.browser?.profile_exists;
  const summary = policy.mode === "autopilot"
    ? `Ищет вакансии. Отклики: ${policy.auto_apply ? "автоматически" : "с подтверждением"}. Автоответы: ${policy.auto_replies ? "включены" : "выключены"}.`
    : policy.mode === "assisted" ? "Ищет вакансии. Каждая отправка требует вашего подтверждения." : "Ищет и готовит, ничего не отправляет.";

  return (
    <div className="animate-rise">
      <PageHeader eyebrow="Автопилот" title="Автопилот"
        description="Помогает находить вакансии и обрабатывать отклики. Ничего не отправляется без вашего разрешения."
        actions={<div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => run("sync", () => apiPost("/api/autopilot/run", {}))}><RefreshCw className="h-3.5 w-3.5" />Проверить сейчас</Button>{policy.enabled && <Button size="sm" variant="outline" onClick={stop} disabled={busy === "stop"}><Pause className="h-3.5 w-3.5" />Остановить</Button>}</div>} />

      {notice && <div className="mb-3 rounded-xl border bg-card p-3 text-[12px] text-muted-foreground">{notice}</div>}
      {error && <div className="mb-3 rounded-xl border border-destructive/25 bg-destructive/5 p-3 text-[12px] text-destructive">{error}</div>}

      <Card className="mb-4 p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div><div className="flex items-center gap-2"><span className={`h-2 w-2 rounded-full ${policy.enabled ? "bg-emerald-500" : "bg-amber-500"}`} /><h2 className="text-[16px] font-semibold">{policy.enabled ? "Автопилот включён" : "Автопилот выключен"}</h2></div><p className="mt-1 max-w-xl text-[12.5px] text-muted-foreground">{summary}</p></div>
          <Button size="sm" onClick={() => setManage(true)}>Управление подключением</Button>
        </div>
        <div className="mt-4 flex flex-wrap gap-4 text-[12px]">
          <label className="flex items-center gap-2"><input type="checkbox" checked={!!policy.enabled} onChange={() => toggle("enabled")} />Автоматический поиск</label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={!!policy.auto_apply} onChange={() => toggle("auto_apply")} disabled={policy.mode !== "autopilot"} />Автоматические отклики</label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={!!policy.auto_replies} onChange={() => toggle("auto_replies")} disabled={policy.mode !== "autopilot"} />Автоматические ответы</label>
          <Button size="sm" variant="outline" onClick={savePolicy} disabled={busy === "policy"}>{busy === "policy" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}Сохранить</Button>
        </div>
      </Card>

      <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-5">
        {[["Отправлено", s.sent], ["Подготовлено", s.prepared], ["Нужно решение", s.pending_decisions],
          ["Ответы", s.messages], ["Ошибки", s.errors]].map(([label, value]) => (
          <Card key={String(label)} className="p-4"><div className="text-[12px] text-muted-foreground">{label}</div><div className="mt-2 text-[24px] font-semibold">{value ?? 0}</div></Card>
        ))}
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="space-y-4">
        <Card>
          <div className="flex items-center gap-2 px-4 pb-1 pt-4"><AlertTriangle className="h-4 w-4 text-amber-500" /><h2 className="text-[14px] font-semibold">Нужно моё решение</h2>{decisions.length ? <Badge variant="warning">{decisions.length}</Badge> : null}</div>
          <div className="space-y-2 p-4 pt-2">
            {decisions.length ? decisions.slice(0, 6).map((d) => (
              <div key={d.id} className="rounded-lg border p-3">
                <div className="flex flex-wrap items-center gap-2"><Badge variant={d.urgency === "high" || d.urgency === "critical" ? "warning" : "default"}>{d.urgency}</Badge><b className="text-[12.5px]">{d.employer || "Работодатель"}</b><span className="text-[11.5px] text-muted-foreground">{d.title}</span></div>
                <div className="mt-1.5 text-[12px]">{d.question}</div>
                {d.reason && <div className="mt-1 text-[11px] text-muted-foreground">{d.reason}</div>}
                <Textarea className="mt-2 min-h-[54px]" placeholder="Комментарий или подтверждённый ответ" value={answers[d.id] || ""} onChange={(e) => setAnswers({ ...answers, [d.id]: e.target.value })} />
                <div className="mt-2 flex gap-2"><Button size="sm" onClick={() => decide(d.id, "approved")}>Подтвердить</Button><Button size="sm" variant="outline" onClick={() => decide(d.id, "postponed")}>Позже</Button><Button size="sm" variant="ghost" onClick={() => decide(d.id, "rejected")}>Отклонить</Button></div>
              </div>
            )) : <div className="flex flex-col items-center justify-center gap-1 rounded-lg border border-dashed px-4 py-8 text-center"><Check className="h-5 w-5 text-emerald-500" /><div className="text-[13px] font-medium">Всё в порядке</div><div className="text-[11.5px] text-muted-foreground">Новых вопросов нет.</div></div>}
          </div>
        </Card>

          <Card>
            <div className="flex items-center justify-between px-4 pb-1 pt-4"><h2 className="text-[14px] font-semibold">История действий</h2><span className="text-[11px] text-muted-foreground">{actions.length}</span></div>
            <div className="space-y-1 p-3">
              {actions.length ? actions.slice(0, 8).map((a) => <div key={a.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg px-2 py-2 text-[11.5px] hover:bg-accent/40"><span><b>{a.kind}</b> · {a.employer || a.title || "система"}</span><span className="text-muted-foreground">{a.result} · {String(a.created_at).slice(0, 16)}</span></div>) : <div className="px-2 py-6 text-center text-[12px] text-muted-foreground">Действий пока нет.</div>}
            </div>
          </Card>
        </div>

        <div className="space-y-4">
          <Card className="p-4">
            <div className="flex items-center justify-between"><h2 className="text-[14px] font-semibold">HeadHunter</h2><Badge variant={connected ? "success" : profileExists ? "warning" : "default"}>{connected ? "подключён" : profileExists ? "вход не проверен" : "не подключён"}</Badge></div>
            <div className="mt-2 text-[11.5px] text-muted-foreground">{connected ? "Сессия браузера сохранена локально." : profileExists ? "Профиль есть, но вход нужно проверить." : "Подключите аккаунт через отдельное окно браузера."}</div>
            <Button size="sm" className="mt-3 w-full" onClick={browserLogin}>{connected ? "Войти заново" : "Подключить hh.ru через браузер"}</Button>
            <Button size="sm" variant="ghost" className="mt-1 w-full" onClick={browserCheck}>Проверить сессию</Button>
          </Card>

          <Card className="p-4">
            <div className="flex items-center justify-between"><h2 className="text-[14px] font-semibold">Письма</h2><Badge variant={data.qwen?.local?.available ? "success" : "warning"}>{data.qwen?.local?.available ? "Qwen локально" : "Qwen недоступна"}</Badge></div>
            <div className="mt-2 text-[11.5px] text-muted-foreground">{data.qwen?.local?.available ? `${data.qwen.local.model} · без оплаты` : data.qwen?.local?.reason || "Проверьте Ollama"}</div>
            <div className="mt-1 text-[11px] text-muted-foreground">
              AITUNNEL: {data.qwen?.cloud?.available ? "готов" : "не подключён"} · расход {Number(data.qwen?.spend?.month_rub || 0).toFixed(2)} ₽
            </div>
            <Button size="sm" variant="outline" className="mt-3 w-full" asChild><Link to="/settings?tab=ai">Настроить генерацию</Link></Button>
          </Card>

          <Card className="p-4">
            <div className="flex items-center gap-2"><Sparkles className="h-4 w-4 text-primary" /><h2 className="text-[14px] font-semibold">Что искать</h2></div>
            <div className="mt-3 space-y-2">
              <label className="block text-[11.5px] text-muted-foreground">Минимальное соответствие<Input type="number" className="mt-1" value={policy.min_match ?? 75} onChange={(e) => setPolicy({ ...policy, min_match: Number(e.target.value) })} /></label>
              <label className="block text-[11.5px] text-muted-foreground">Лимит откликов в день<Input type="number" className="mt-1" value={policy.daily_limit ?? 5} onChange={(e) => setPolicy({ ...policy, daily_limit: Number(e.target.value) })} /></label>
              <label className="block text-[11.5px] text-muted-foreground">Режим<Select className="mt-1" value={policy.mode} onChange={(e) => setPolicy({ ...policy, mode: e.target.value, ...(e.target.value !== "autopilot" ? { auto_apply: false, auto_replies: false } : {}) })}>{MODES.map(([v, l, h]) => <option key={v} value={v}>{l} — {h}</option>)}</Select></label>
              <Button size="sm" variant="outline" className="w-full" onClick={savePolicy}>Сохранить настройки</Button>
            </div>
          </Card>
        </div>
      </div>

      <Dialog open={manage} onOpenChange={setManage}>
        <DialogContent className="max-h-[88vh] max-w-2xl overflow-auto">
          <DialogHeader><DialogTitle>Подключение HeadHunter</DialogTitle></DialogHeader>
          <div className="space-y-4 text-[12px]">
            <div className="rounded-lg border p-3">
              <div className="flex items-center justify-between"><b>Браузерная сессия</b><Badge variant={browser?.logged_in ? "success" : "warning"}>{browser?.logged_in ? "авторизована" : "не проверена"}</Badge></div>
              <div className="mt-1 text-[11px] text-muted-foreground">{browser?.profile_dir || data.browser?.profile_dir}</div>
              <div className="mt-3 flex flex-wrap gap-2"><Button size="sm" onClick={browserLogin}>Войти через браузер</Button><Button size="sm" variant="outline" onClick={browserCheck}>Проверить сессию</Button><Button size="sm" variant="outline" onClick={browserHistory}>Сохранить историю</Button></div>
            </div>
            <details className="rounded-lg border p-3">
              <summary className="cursor-pointer font-medium">OAuth / API и служебные проверки</summary>
              <div className="mt-3 flex flex-wrap gap-2"><Button size="sm" variant="outline" onClick={() => run("history", () => apiPost("/api/autopilot/sync-history", { pages: 500 }))}>Синхронизировать историю</Button><Button size="sm" variant="ghost" onClick={() => run("diagnostics", async () => { const r = await apiPost<any>("/api/hh/oauth/diagnostics", {}); return r.ok ? "Проверки прошли" : (r.reason || "Есть ограничение"); })}>Проверить API</Button></div>
              <div className="mt-2"><Button size="sm" variant="ghost" onClick={() => { setTech(true); }}>Покивань технические данные</Button></div>
              {tech && <pre className="mt-2 max-h-40 overflow-auto rounded-md bg-muted p-2 text-[10px]">{JSON.stringify({ oauth: data.oauth?.connected, credentials: data.oauth?.credentials_source, storage: data.oauth?.storage }, null, 2)}</pre>}
            </details>
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-[11.5px] text-muted-foreground">Отправка откликов никогда не подтверждается только открытием страницы. Для внешних форм используется подготовка и ручное подтверждение.</div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
