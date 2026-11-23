import { useEffect, useMemo, useState } from "react";
import {
  Check, ChevronDown, Copy, ExternalLink, Loader2, MessageSquare, RefreshCw, Rocket,
  Send, ShieldCheck, Sparkles, Wallet,
} from "lucide-react";
import { apiGet, apiPost } from "@/lib/api";
import { StartupLead, StartupStats } from "@/lib/types";
import { PageHeader, ErrorState, LoadingState } from "@/components/PageChrome";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type Data = { items: StartupLead[]; stats: StartupStats; kinds: Record<string, string>; states: Record<string, string> };

const TABS = [
  ["all", "Все"], ["verified_vacancy", "Открытые вакансии"], ["paid_internship", "Оплачиваемые стажировки"],
  ["paid_project", "Проектная работа"], ["company_hiring", "Набирают команду"],
  ["potential_employer", "Потенциальные работодатели"], ["drafts", "Подготовленные обращения"],
  ["contacted", "Контакты и ответы"],
] as const;

const FILTERS = [
  ["ai", "AI/ML"], ["python", "Python/backend"], ["data", "Data"], ["llm", "LLM/RAG"],
  ["parttime", "part-time"], ["remote", "remote"], ["moscow", "Москва"],
  ["paid", "Оплачиваемые"], ["internship", "Стажировки"], ["junior", "Junior"],
] as const;

const MODE: Record<string, string> = { remote: "удалённо", hybrid: "гибрид", office: "офис" };

function scoreTone(value: number) {
  return value >= 70 ? "success" : value >= 45 ? "warning" : "default";
}

function matches(lead: StartupLead, filter: string) {
  const text = `${lead.company} ${lead.product} ${lead.description} ${lead.role} ${lead.hiring_status} ${lead.stack.join(" ")} ${lead.location}`.toLowerCase();
  switch (filter) {
    case "ai": return /(^|\s)(ai|ml|llm|rag|нейросет|искусствен)/.test(text);
    case "python": return /python|backend|бэкенд|fastapi/.test(text);
    case "data": return /data|данн|аналит/.test(text);
    case "llm": return /llm|rag|gpt|embedding/.test(text);
    case "parttime": return /part-?time|частичн|подработ/.test(text);
    case "remote": return lead.work_mode === "remote" || /удал[её]н/.test(text);
    case "moscow": return /москв/.test(lead.location.toLowerCase());
    case "paid": return lead.pay_status === "paid";
    case "internship": return lead.kind === "paid_internship" || /стаж|intern/.test(text);
    case "junior": return /junior|джуниор|стаж|начинающ/.test(text);
    default: return true;
  }
}

export default function StartupsPage() {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [tab, setTab] = useState<string>("all");
  const [active, setActive] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [openId, setOpenId] = useState("");

  async function load() {
    try {
      setData(await apiGet<Data>("/api/startups"));
      setError("");
    } catch (e) { setError((e as Error).message); }
  }
  useEffect(() => { load(); }, []);

  const items = useMemo(() => {
    if (!data) return [];
    return data.items.filter((lead) => {
      if (tab === "drafts" && !(lead.status === "draft_ready" || lead.status === "awaiting_approval")) return false;
      if (tab === "contacted" && !["contacted", "replied", "interview", "offer", "rejected"].includes(lead.status)) return false;
      if (!["all", "drafts", "contacted"].includes(tab) && lead.kind !== tab) return false;
      if (active.length && !active.every((f) => matches(lead, f))) return false;
      if (search) {
        const q = search.toLowerCase();
        if (!`${lead.company} ${lead.product} ${lead.description} ${lead.stack.join(" ")}`.toLowerCase().includes(q)) return false;
      }
      return true;
    });
  }, [data, tab, active, search]);

  async function refresh(force: boolean) {
    setBusy("refresh"); setNotice(""); setError("");
    try {
      const r = await apiPost<any>("/api/startups/refresh", { force });
      const sources = Object.entries(r.sources || {}).map(([k, v]: any) => `${k}: ${v.details ?? 0} карточек`).join(", ");
      setNotice(`Проверено: ${r.added} новых, ${r.updated} обновлено, черновиков ${r.drafts}. ${sources}`);
      await load();
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  async function rescore() {
    setBusy("rescore");
    try { await apiPost("/api/startups/rescore"); await load(); setNotice("Оценки пересчитаны без повторных запросов."); }
    catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  async function act(id: string, action: string, payload: Record<string, unknown> = {}) {
    setBusy(`${action}-${id}`); setNotice(""); setError("");
    try {
      const r = await apiPost<any>(`/api/startups/${id}/${action}`, payload);
      if (r.warning) setNotice(r.warning);
      else if (r.state_label) setNotice(`Статус: ${r.state_label}`);
      else if (r.ok) setNotice("Готово.");
      await load();
      if (action === "draft") setOpenId(id);
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  async function copy(text: string) {
    try { await navigator.clipboard.writeText(text); setNotice("Текст скопирован."); }
    catch { setNotice("Браузер не разрешил копирование — выделите текст вручную."); }
  }
  function contactHref(lead: StartupLead) {
    if (lead.contact.startsWith("mailto:")) return lead.contact;
    if (lead.contact) return lead.contact;
    return lead.website || lead.source_url;
  }

  if (error && !data) return <ErrorState error={error} onRetry={load} />;
  if (!data) return <LoadingState rows={5} />;
  const stats = data.stats;

  return (
    <div className="animate-rise">
      <PageHeader eyebrow="Стартапы" title="Маленькие команды и AI-стартапы"
        description="Опубликованные возможности небольших команд. Fit, подтверждённость найма и оплата показаны раздельно. Компания без вакансии — не вакансия."
        actions={<>
          <Button size="sm" variant="outline" onClick={() => refresh(false)} disabled={!!busy}>
            {busy === "refresh" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}Обновить
          </Button>
          <Button size="sm" variant="ghost" onClick={rescore} disabled={!!busy}>Пересчитать</Button>
        </>} />

      <div className="mb-3 grid grid-cols-2 gap-2 text-[11.5px] sm:grid-cols-4 xl:grid-cols-6">
        {[
          ["Компаний", stats.total], ["Оплачиваемых", stats.paid], ["Набирают", stats.hiring],
          ["Без оплаты", stats.unpaid], ["Черновики", stats.drafts], ["Отправлено", stats.contacted],
        ].map(([label, value]) => (
          <div key={String(label)} className="rounded-md border bg-card px-2.5 py-2">
            <div className="text-muted-foreground">{label}</div><b className="text-[15px]">{value}</b>
          </div>
        ))}
      </div>

      {notice && <div className="mb-3 rounded-md border bg-card p-3 text-[12px] text-muted-foreground">{notice}</div>}
      {error && <div className="mb-3 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-[12px] text-destructive">{error}</div>}

      <div className="mb-3 flex flex-wrap gap-1.5">
        {TABS.map(([key, label]) => (
          <Button key={key} size="sm" variant={tab === key ? "default" : "outline"}
            onClick={() => setTab(key)}>
            {label}{key === "drafts" ? ` (${stats.drafts})` : ""}
            {key === "contacted" ? ` (${stats.contacted})` : ""}
          </Button>
        ))}
      </div>
      <div className="mb-3 flex flex-wrap items-center gap-1.5">
        {FILTERS.map(([key, label]) => (
          <button key={key} onClick={() => setActive((prev) => prev.includes(key) ? prev.filter((x) => x !== key) : [...prev, key])}
            className={`rounded-full border px-2.5 py-1 text-[11.5px] transition-colors ${active.includes(key) ? "border-primary bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent"}`}>
            {label}
          </button>
        ))}
        <div className="ml-auto w-full sm:w-64"><Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Компания, стек, задача…" /></div>
      </div>

      <div className="text-[11.5px] text-muted-foreground">
        Оценки — экспертные, не вероятность оффера. «Оплата не подтверждена» означает, что источник её не указал.
      </div>

      <div className="mt-3 space-y-3">
        {items.length ? items.map((lead) => {
          const expanded = openId === lead.id;
          const confirmed = ["contacted", "replied", "interview", "offer"].includes(lead.status);
          return (
            <Card key={lead.id}>
              <CardContent className="p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge variant={lead.is_paid ? "success" : lead.pay_status === "unpaid" ? "default" : "warning"}>{lead.pay_label}</Badge>
                      <Badge>{lead.kind_label}</Badge>
                      {lead.readiness_label ? <Badge variant={lead.readiness === "ready_to_review" ? "success" : "warning"}>{lead.readiness_label}</Badge> : null}
                      <Badge variant={lead.status === "draft_ready" ? "warning" : lead.status === "contacted" ? "success" : "default"}>{lead.fit_score < 45 && !["contacted", "replied"].includes(lead.status) ? "Слабое соответствие" : lead.state_label}</Badge>
                    </div>
                    <h3 className="mt-2 text-[16px] font-semibold">{lead.company}</h3>
                    <div className="mt-0.5 text-[12px] text-muted-foreground">
                      {lead.role || lead.product}{lead.location ? ` · ${lead.location}` : ""}{lead.work_mode ? ` · ${MODE[lead.work_mode] || lead.work_mode}` : ""}
                      {lead.source ? ` · ${lead.source}` : ""}
                    </div>
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-center">
                    <div className="rounded-md border px-2 py-1.5"><div className="text-[10px] text-muted-foreground">Startup Fit</div><b className="text-[15px]">{Math.round(lead.fit_score)}</b></div>
                    <div className="rounded-md border px-2 py-1.5"><div className="text-[10px] text-muted-foreground">Hiring Evidence</div><b className="text-[15px]">{Math.round(lead.hiring_evidence)}</b></div>
                    <div className="rounded-md border px-2 py-1.5"><div className="text-[10px] text-muted-foreground">Compensation</div><b className="text-[15px]">{Math.round(lead.compensation_confidence)}</b></div>
                  </div>
                </div>

                <p className="mt-3 line-clamp-3 text-[12.5px] text-muted-foreground">{lead.description || lead.hiring_status}</p>
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                  {lead.stack.slice(0, 8).map((s) => <Badge key={s} variant="primary">{s}</Badge>)}
                  {lead.founders?.length ? <span className="text-[11px] text-muted-foreground">Автор: {lead.founders.join(", ")}</span> : null}
                </div>

                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <Button size="sm" variant="outline" asChild><a href={lead.website || lead.source_url} target="_blank" rel="noopener noreferrer"><ExternalLink className="h-3.5 w-3.5" />Открыть компанию</a></Button>
                  <Button size="sm" variant="ghost" asChild><a href={lead.evidence_url} target="_blank" rel="noopener noreferrer">Первоисточник</a></Button>
                  <Button size="sm" variant="ghost" onClick={() => setOpenId(expanded ? "" : lead.id)}>
                    <ChevronDown className={`h-3.5 w-3.5 transition-transform ${expanded ? "rotate-180" : ""}`} />Обоснование
                  </Button>
                  <div className="flex-1" />
                  <Button size="sm" onClick={() => act(lead.id, "draft")} disabled={!!busy || confirmed}>
                    {busy === `draft-${lead.id}` ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                    {lead.outreach ? "Обновить обращение" : "Подготовить обращение"}
                  </Button>
                </div>

                {expanded && (
                  <div className="mt-3 grid gap-3 rounded-md border bg-muted/20 p-3 text-[12px] sm:grid-cols-3">
                    <div>
                      <div className="mb-1 flex items-center gap-1 font-medium"><Sparkles className="h-3.5 w-3.5" />Startup Fit</div>
                      <div className="text-muted-foreground">Близость направления и задач к подтверждённым навыкам.</div>
                      <div className="mt-1">Совпало: <b>{lead.rationale.matches?.join(", ") || "—"}</b></div>
                      <div className="text-muted-foreground">Не подтверждено: {lead.rationale.gaps?.join(", ") || "—"}</div>
                    </div>
                    <div>
                      <div className="mb-1 flex items-center gap-1 font-medium"><ShieldCheck className="h-3.5 w-3.5" />Hiring Evidence</div>
                      <div className="text-muted-foreground">{lead.hiring_status}</div>
                      <div className="mt-1 text-[11px]">Проверено: {lead.last_checked_at?.slice(0, 10) || "—"}</div>
                    </div>
                    <div>
                      <div className="mb-1 flex items-center gap-1 font-medium"><Wallet className="h-3.5 w-3.5" />Оплата</div>
                      <div className="text-muted-foreground">{lead.pay_label}</div>
                      <div className="mt-1">{lead.rationale.notes?.join(" ")}</div>
                      {lead.rationale.assumptions?.length ? <div className="mt-1 text-[11px] text-muted-foreground">{lead.rationale.assumptions[0]}</div> : null}
                    </div>
                  </div>
                )}

                {lead.outreach && (
                  <div className="mt-3 rounded-md border p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="text-[12.5px] font-medium">{lead.outreach_subject || "Обращение"}</div>
                      <div className="flex gap-1.5">
                        <Button size="sm" variant="ghost" onClick={() => copy(lead.outreach)}><Copy className="h-3.5 w-3.5" />Скопировать</Button>
                        <Button size="sm" variant="outline" asChild><a href={contactHref(lead)} target="_blank" rel="noopener noreferrer">Открыть контакт</a></Button>
                        {lead.status !== "contacted" && lead.status !== "replied" && (
                          <Button size="sm" onClick={() => {
                            if (window.confirm("Подтверждаете, что обращение реально отправлено вручную?")) act(lead.id, "state", { state: "contacted", confirm: true });
                          }}><Send className="h-3.5 w-3.5" />Подтвердить отправку</Button>
                        )}
                        {lead.status === "contacted" && (
                          <Button size="sm" variant="outline" onClick={() => act(lead.id, "state", { state: "replied" })}><MessageSquare className="h-3.5 w-3.5" />Отметить ответ</Button>
                        )}
                        {["replied", "interview", "offer"].includes(lead.status) && <Badge variant="success"><Check className="h-3 w-3" />{lead.state_label}</Badge>}
                      </div>
                    </div>
                    <pre className="mt-2 whitespace-pre-wrap break-words font-sans text-[12px] leading-relaxed text-muted-foreground">{lead.outreach}</pre>
                  </div>
                )}
              </CardContent>
            </Card>
          );
        }) : <div className="rounded-lg border bg-card p-8 text-center text-sm text-muted-foreground">
          По выбранным фильтрам ничего нет. Снимите фильтры или обновите источники.
        </div>}
      </div>
    </div>
  );
}
