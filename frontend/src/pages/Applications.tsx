import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Check, ChevronRight, Clock3, ExternalLink, FileText, Loader2, MessageSquare, Send, ShieldCheck, X } from "lucide-react";
import { apiGet, apiPost } from "@/lib/api";
import { ApplicationsData, StartupLead } from "@/lib/types";
import { dt, letterOrigin, QUEUE_LABELS } from "@/lib/utils";
import { PageHeader, EmptyState, ErrorState, LoadingState } from "@/components/PageChrome";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input, Textarea } from "@/components/ui/input";

const OPEN = ["queued", "manual_review", "requires_user_action", "approved", "form_filling", "opening_application", "ready_to_review", "submission_unverified"];
const CLOSED = ["cancelled", "blocked", "rejected", "withdrawn", "failed"];
const TABS = [["all", "Все"], ["waiting", "Ожидают"], ["invited", "Приглашения"], ["closed", "Завершённые"], ["startups", "Стартапы"]] as const;

function statusLabel(a: any) {
  if (a.state === "submitted") return "Отправлено";
  if (a.state === "approved") return "Одобрено";
  if (a.state === "queued") return "В очереди";
  if (a.state === "manual_review") return "Нужна проверка";
  if (a.state === "requires_user_action") return "Нужно действие";
  if (a.state === "blocked") return "Остановлено";
  if (a.state === "cancelled") return "Отменено";
  if (a.state === "submission_unverified") return "Отправка не подтверждена";
  if (a.state === "ready_to_review") return "Проверьте форму";
  return QUEUE_LABELS[a.state] || a.readiness_label || a.state;
}

export default function ApplicationsPage() {
  const [data, setData] = useState<ApplicationsData | null>(null);
  const [startups, setStartups] = useState<StartupLead[]>([]);
  const [messages, setMessages] = useState<any>(null);
  const [tab, setTab] = useState<string>("all");
  const [selected, setSelected] = useState<any>(null);
  const [selectedLead, setSelectedLead] = useState<StartupLead | null>(null);
  const [contact, setContact] = useState<any>({});
  const [letter, setLetter] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function load() {
    try {
      const [a, s, m, r] = await Promise.all([
        apiGet<ApplicationsData>("/api/applications"),
        apiGet<any>("/api/startups"),
        apiGet<any>("/api/messages"),
        apiGet<any>("/api/resume"),
      ]);
      setData(a); setStartups(s.items || []); setMessages(m); setContact(r.resume?.contact || {}); setError("");
    } catch (e) { setError((e as Error).message); }
  }
  useEffect(() => { load(); }, []);

  async function run(label: string, fn: () => Promise<any>) {
    setBusy(label); setError(""); setNotice("");
    try { await fn(); await load(); } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  async function appAction(id: number, action: string, payload: Record<string, unknown> = {}) {
    await run(`${action}-${id}`, async () => {
      const r = await apiPost<any>(`/api/queue/${id}/${action}`, { confirm: false, ...payload });
      setNotice(r?.reason || r?.note || "Готово");
    });
  }
  async function confirmSent(id: number) {
    if (!window.confirm("Вы действительно отправили отклик на площадке? Это ручное подтверждение.")) return;
    await run(`confirm-${id}`, async () => {
      await apiPost(`/api/queue/${id}/confirm-submitted`, { confirm: true, method: "manual", source: "user_attestation" });
      setNotice("Отправка подтверждена.");
    });
  }
  async function saveLetter() {
    if (!selected || !letter.trim()) return;
    await run("letter", async () => {
      await apiPost("/api/jobs/letter", { id: selected.job_id, text: letter });
      setNotice("Письмо сохранено. Проверьте и одобрите заново.");
    });
  }
  async function saveContact() {
    await run("contact", async () => {
      await apiPost("/api/resume/contact", contact);
      setNotice("Контактные данные сохранены.");
    });
  }
  async function prepareSubmission() {
    if (!selected) return;
    await run("prepare-submission", async () => {
      const r = await apiPost<any>("/api/submission/prepare", { job_id: selected.job_id });
      setNotice(r?.reason || (r?.status === "ready_to_review" ? "Форма подготовлена, отправка не выполнялась." : "Готово."));
    });
  }
  async function leadAction(id: string, state: string) {
    if (state === "contacted" && !window.confirm("Вы действительно отправили обращение?")) return;
    await run(`lead-${id}`, async () => {
      await apiPost(`/api/startups/${id}/state`, { state, confirm: state === "contacted" });
      setNotice("Статус обращения обновлён.");
    });
  }
  function openApp(a: any) { setSelected(a); setLetter(a.letter || ""); }

  if (error && !data) return <ErrorState error={error} onRetry={load} />;
  if (!data) return <LoadingState rows={5} />;
  const apps = data.items || [];
  const counts = data.stats?.counts || {};
  const waiting = apps.filter((a) => OPEN.includes(a.state));
  const invited = apps.filter((a) => ["invited", "interview", "offer"].includes(a.outcome || "") || ["invited", "interview", "offer"].includes(a.state));
  const closed = apps.filter((a) => CLOSED.includes(a.state));
  const base = tab === "waiting" ? waiting : tab === "invited" ? invited : tab === "closed" ? closed : apps.filter((a) => tab !== "startups");
  const contactMissing = [["first_name", "имя"], ["last_name", "фамилия"], ["email", "email"]]
    .filter(([k]) => !String(contact[k] || "").trim()).map(([, label]) => label);
  const leads = startups.filter((l) => l.outreach);

  return (
    <div className="animate-rise">
      <PageHeader eyebrow="Отклики" title="Мои отклики"
        description="Что отправлено, что ждёт ответа и где нужно ваше действие."
        actions={<Button size="sm" variant="outline" asChild><a href="/api/export/queue.csv">Экспорт</a></Button>} />

      <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Card className="p-4"><div className="text-[12px] text-muted-foreground">В работе</div><div className="mt-2 text-[24px] font-semibold">{waiting.length}</div></Card>
        <Card className="p-4"><div className="text-[12px] text-muted-foreground">Отправлено</div><div className="mt-2 text-[24px] font-semibold">{data.stats?.sent_today ?? 0}</div></Card>
        <Card className="p-4"><div className="text-[12px] text-muted-foreground">Приглашения</div><div className="mt-2 text-[24px] font-semibold">{invited.length}</div></Card>
        <Card className="p-4"><div className="text-[12px] text-muted-foreground">Не хватает полей профиля</div><div className="mt-2 text-[24px] font-semibold">{contactMissing.length}</div></Card>
      </div>

      {contactMissing.length > 0 && (
        <Card className="mb-4 border-amber-500/30 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div><b className="text-[13px]">Добавьте контактные данные</b><div className="text-[11.5px] text-muted-foreground">Не хватает: {contactMissing.join(", ")}. Это нужно для форм и писем.</div></div>
            <Button size="sm" variant="outline" onClick={saveContact} disabled={busy === "contact"}>{busy === "contact" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}Сохранить</Button>
          </div>
          <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Input placeholder="Имя" value={contact.first_name || ""} onChange={(e) => setContact({ ...contact, first_name: e.target.value })} />
            <Input placeholder="Фамилия" value={contact.last_name || ""} onChange={(e) => setContact({ ...contact, last_name: e.target.value })} />
            <Input placeholder="Email" value={contact.email || ""} onChange={(e) => setContact({ ...contact, email: e.target.value })} />
            <Input placeholder="Телефон" value={contact.phone || ""} onChange={(e) => setContact({ ...contact, phone: e.target.value })} />
          </div>
        </Card>
      )}

      {error && <div className="mb-3 rounded-xl border border-destructive/25 bg-destructive/5 p-3 text-[12px] text-destructive">{error}</div>}
      {notice && <div className="mb-3 rounded-xl border bg-card p-3 text-[12px] text-muted-foreground">{notice}</div>}

      <div className="mb-3 flex flex-wrap gap-1.5">
        {TABS.map(([key, label]) => (
          <Button key={key} size="sm" variant={tab === key ? "default" : "outline"} onClick={() => setTab(key)}>
            {label}{key === "waiting" ? ` (${waiting.length})` : key === "invited" ? ` (${invited.length})` : key === "startups" ? ` (${leads.length})` : ""}
          </Button>
        ))}
      </div>

      {tab === "startups" ? (
        <Card className="divide-y">
          {leads.length ? leads.map((lead) => (
            <button key={lead.id} onClick={() => setSelectedLead(lead)} className="flex w-full items-center gap-3 px-4 py-4 text-left hover:bg-accent/40">
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-[12px] font-semibold text-primary">{lead.company.slice(0, 1)}</div>
              <div className="min-w-0 flex-1"><div className="text-[13.5px] font-medium">{lead.company}</div><div className="truncate text-[11.5px] text-muted-foreground">{lead.kind_label} · {lead.pay_label}</div></div>
              <Badge variant={lead.status === "contacted" ? "success" : "default"}>{lead.state_label}</Badge>
              <ChevronRight className="h-4 w-4 text-muted-foreground" />
            </button>
          )) : <EmptyState title="Обращений к стартапам нет" description="Подготовьте обращение на странице стартапов." />}
        </Card>
      ) : (
        <Card className="divide-y">
          {base.length ? base.filter((a) => tab !== "invited" || invited.includes(a)).map((a) => (
            <button key={a.id} onClick={() => openApp(a)} className="flex w-full items-center gap-3 px-4 py-4 text-left hover:bg-accent/40">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-2"><span className="truncate text-[13.5px] font-medium">{a.title}</span><span className="text-[11.5px] text-muted-foreground">{a.company || "Компания не указана"}</span></div>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-[11.5px] text-muted-foreground"><span>{dt(a.updated_at)}</span>{a.readiness_reason && <><span>·</span><span className="truncate">{a.readiness_reason}</span></>}</div>
              </div>
              <Badge variant={a.state === "submitted" ? "success" : a.state === "manual_review" || a.state === "requires_user_action" ? "warning" : "default"}>{statusLabel(a)}</Badge>
              <ChevronRight className="h-4 w-4 text-muted-foreground" />
            </button>
          )) : <EmptyState icon={<Send className="h-5 w-5" />} title="Здесь пока пусто" description="Отклики появятся после подготовки писем." />}
        </Card>
      )}

      <Dialog open={!!selected} onOpenChange={(o) => !o && setSelected(null)}>
        <DialogContent className="max-h-[88vh] max-w-3xl overflow-auto">
          <DialogHeader><DialogTitle>{selected?.title}</DialogTitle></DialogHeader>
          {selected && (
            <div className="space-y-4 text-[12.5px]">
              <div className="flex flex-wrap items-center gap-2">
                <Badge>{statusLabel(selected)}</Badge><span className="text-muted-foreground">{selected.company} · {selected.source}</span>
                <div className="flex-1" />
                {selected.url && <Button size="sm" variant="outline" asChild><a href={selected.apply_url || selected.url} target="_blank" rel="noopener noreferrer"><ExternalLink className="h-3.5 w-3.5" />Оригинал</a></Button>}
              </div>
              {selected.readiness_reason && <div className="rounded-lg border bg-muted/30 p-3">{selected.readiness_reason}</div>}
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="rounded-lg border p-3"><b>Отклик</b><div className="mt-1 text-muted-foreground">Обновлён: {dt(selected.updated_at)}</div><div className="text-muted-foreground">Резюме: {selected.resume_variant || "не выбрано"}</div><div className="text-muted-foreground">Канал: {selected.channel}</div></div>
                <div className="rounded-lg border p-3"><b>Сопроводительное письмо</b>{letterOrigin(selected.letter_source) && <div className="text-[10.5px] text-muted-foreground">{letterOrigin(selected.letter_source)}</div>}<div className="mt-1 max-h-[150px] overflow-auto whitespace-pre-wrap text-[11.5px] text-muted-foreground">{selected.letter || "Письмо не подготовлено"}</div></div>
              </div>
              <div className="flex flex-wrap gap-2">
                {["ready_to_review", "ready_to_submit"].includes(selected.readiness) && <Button size="sm" onClick={() => appAction(selected.id, "approve")}><Check className="h-3.5 w-3.5" />Одобрить</Button>}
                {!["submitted", "cancelled", "blocked"].includes(selected.state) && <Button size="sm" variant="outline" onClick={() => appAction(selected.id, "form")}><FileText className="h-3.5 w-3.5" />Подготовить форму</Button>}
                {!["submitted", "cancelled", "blocked"].includes(selected.state) && <Button size="sm" variant="outline" onClick={prepareSubmission} disabled={busy === "prepare-submission"}><Send className="h-3.5 w-3.5" />Подготовить отправку</Button>}
                {!["submitted", "cancelled", "blocked"].includes(selected.state) && <Button size="sm" variant="outline" onClick={() => confirmSent(selected.id)}><Send className="h-3.5 w-3.5" />Я отправил</Button>}
                {selected.state === "submitted" && <Button size="sm" variant="outline" onClick={() => appAction(selected.id, "outcome", { outcome: "replied" })}><MessageSquare className="h-3.5 w-3.5" />Отметить ответ</Button>}
                <Button size="sm" variant="ghost" onClick={() => appAction(selected.id, "cancel")}><X className="h-3.5 w-3.5" />Отменить</Button>
              </div>
              <div>
                <div className="mb-1 font-medium">Письмо</div>
                <Textarea value={letter} onChange={(e) => setLetter(e.target.value)} className="min-h-[150px]" />
                <Button size="sm" className="mt-2" onClick={saveLetter} disabled={busy === "letter"}>Сохранить письмо</Button>
              </div>
              {selected.history?.length ? <details><summary className="cursor-pointer text-muted-foreground">История событий</summary><div className="mt-2 space-y-1 text-[11.5px] text-muted-foreground">{selected.history.map((h: any, i: number) => <div key={i}>{dt(h.created_at)} · {h.type}</div>)}</div></details> : null}
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={!!selectedLead} onOpenChange={(o) => !o && setSelectedLead(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader><DialogTitle>{selectedLead?.company}</DialogTitle></DialogHeader>
          {selectedLead && <div className="space-y-3 text-[12.5px]">
            <div className="flex flex-wrap gap-2"><Badge>{selectedLead.kind_label}</Badge><Badge variant={selectedLead.is_paid ? "success" : "default"}>{selectedLead.pay_label}</Badge><Badge>{selectedLead.state_label}</Badge></div>
            <div className="whitespace-pre-wrap rounded-lg border bg-muted/30 p-3 text-[11.5px] leading-relaxed">{selectedLead.outreach}</div>
            <div className="flex flex-wrap gap-2">
              {selectedLead.contact && <Button size="sm" variant="outline" asChild><a href={selectedLead.contact} target="_blank" rel="noopener noreferrer">Открыть контакт</a></Button>}
              {selectedLead.status !== "contacted" && <Button size="sm" onClick={() => leadAction(selectedLead.id, "contacted")}><Send className="h-3.5 w-3.5" />Я отправил</Button>}
              {selectedLead.status === "contacted" && <Button size="sm" variant="outline" onClick={() => leadAction(selectedLead.id, "replied")}><MessageSquare className="h-3.5 w-3.5" />Ответ</Button>}
              <Button size="sm" variant="ghost" asChild><Link to="/startups">Все стартапы</Link></Button>
            </div>
          </div>}
        </DialogContent>
      </Dialog>
    </div>
  );
}
