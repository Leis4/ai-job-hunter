import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft, Bookmark, BookmarkCheck, ExternalLink, FileText, Loader2, RefreshCw, Send, Sparkles, Wand2,
} from "lucide-react";
import { apiGet, apiPost } from "@/lib/api";
import { Job } from "@/lib/types";
import { dt, letterOrigin, money, MODE_LABELS, QUEUE_LABELS, SOURCE_LABELS, STATUS_LABELS, FACTOR_LABELS } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScoreRing } from "@/components/ScoreRing";
import { EligibilityBadge, jobVerdict, realismVariant } from "@/components/JobCard";
import { ErrorState, LoadingState } from "@/components/PageChrome";

type Detail = Job & {
  applications: any[]; messages: any[]; events: any[];
  variant: string; variant_label: string; calibrated_probability?: number | null;
  resume_preview_url: string;
  letter_checks?: { fallback_reason?: string; critique?: { verdict?: string; unsupported_claims?: string[] } };
  publications: { copy_id: string; canonical_id: string; source: string; source_id: string;
    url: string; title: string; company: string; similarity: number }[];
};

export default function VacancyDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [job, setJob] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [letter, setLetter] = useState("");
  const [busy, setBusy] = useState("");
  const [saved, setSaved] = useState("");
  const [genOpen, setGenOpen] = useState(false);
  const [enriching, setEnriching] = useState(false);

  async function load() {
    try {
      const data = await apiGet<Detail>(`/api/jobs/${id}`);
      setJob(data); setLetter(data.letter); setError("");
    } catch (e) { setError((e as Error).message); }
  }
  useEffect(() => { load(); }, [id]);

  async function enrich() {
    setEnriching(true); setError("");
    try {
      const result = await apiPost<any>("/api/jobs/enrich", { id: job?.id });
      await load();
      setSaved(result?.url ? "Подробности обновлены" : "Подробности не удалось получить");
    } catch (e) { setError((e as Error).message); }
    finally { setEnriching(false); }
  }

  async function action(name: string, fn: () => Promise<any>) {
    setBusy(name); setSaved("");
    try { await fn(); await load(); setSaved("Готово"); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(""); }
  }

  if (error && !job) return <ErrorState error={error} onRetry={load} />;
  if (!job) return <LoadingState rows={5} />;

  const s = job.scores;
  const verdict = jobVerdict(job);
  const rejected = job.eligibility === "rejected";
  const threshold = job.threshold ?? 65;
  const factors = Object.entries(job.factors || {})
    .map(([key, f]) => ({ key, value: Number(f.value ?? 0), weight: Number(f.weight ?? 0) }))
    .sort((a, b) => b.weight - a.weight);

  return (
    <div className="animate-rise">
      <button onClick={() => navigate(-1)} className="mb-3 flex items-center gap-1 text-[12.5px] text-muted-foreground hover:text-foreground"><ArrowLeft className="h-3.5 w-3.5" />Назад</button>
      <div className="mb-4 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="mb-1 text-[12px] text-muted-foreground">{job.company}{job.location ? ` · ${job.location}` : ""}{job.work_mode ? ` · ${MODE_LABELS[job.work_mode] || job.work_mode}` : ""}</div>
          <h1 className="text-[26px] font-semibold leading-tight tracking-[-0.02em]">{job.title}</h1>
          <div className="mt-2 flex flex-wrap gap-1.5">
            <EligibilityBadge job={job} />
            <Badge>{STATUS_LABELS[job.status] || job.status}</Badge>
            <Badge variant="primary">{money(job.salary_min, job.salary_max, job.currency)}</Badge>
            <Badge>{job.source}</Badge>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => action("fav", async () => { await apiPost("/api/jobs/favorite", { id: job.id }); })}>
            {job.favorite ? <BookmarkCheck className="h-4 w-4" /> : <Bookmark className="h-4 w-4" />}Избранное
          </Button>
          <Button variant="outline" asChild><a href={job.apply_url || job.url} target="_blank" rel="noopener noreferrer"><ExternalLink className="h-4 w-4" />Оригинал</a></Button>
        </div>
      </div>

      {rejected && (
        <div className="mb-4 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          <b>Не проходит обязательные требования:</b> {job.failed_constraints.join(", ") || "ограничения вакансии"}. Навыки могут совпадать, но отклик не рекомендуется.
        </div>
      )}
      {!rejected && job.eligibility === "review" && <div className="mb-4 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-[13px] text-amber-800 dark:text-amber-200">Часть условий требует проверки. Это не запрет на ручное рассмотрение, но автоматическая очередь исключена.</div>}
      {!!job.schedule?.signals?.length && <div className="mb-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-[12.5px]">График: {job.schedule.signals.join("; ")}</div>}
      {job.location_mismatch && <div className="mb-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-[12.5px]">Город или формат работы может требовать переезда. Уточни присутствие в офисе.</div>}
      {job.category === "opportunity" && (
        <div className="mb-4 rounded-lg border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-[13px] text-sky-800 dark:text-sky-200">
          <b>Перспективная вакансия.</b> {(job.category_reasons || []).join("; ")}. Можно подготовить отклик вручную после проверки.
        </div>
      )}
      {job.eligibility === "weak" && (        <div className="mb-4 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-[13px] text-amber-800 dark:text-amber-200">
          <b>Низкое соответствие профилю.</b> Навыки {Math.round(s.skill_match || 0)}/100 или реалистичность приглашения {Math.round(job.realism?.value || 0)}/100 ниже порога. В очередь автоматически не ставится.
        </div>
      )}
      {job.commercial_experience?.required && (
        <div className="mb-4 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          <b>Нужен коммерческий/production-опыт:</b> {(job.commercial_experience.signals || []).join(", ")}. У кандидата подтверждено {Math.round((job.commercial_experience.candidate_months || 0) / 12 * 10) / 10} г. коммерческого опыта.
        </div>
      )}
      {!rejected && job.eligibility === "eligible" && !job.eligible && (
        <div className="mb-4 rounded-lg border bg-card px-4 py-3 text-[13px] text-muted-foreground">
          Обязательные требования выполнены, но Match Score <b className="text-foreground">{Math.round(job.score)}</b> ниже порога <b className="text-foreground">{threshold}</b>. Такие вакансии не попадают в очередь автоматически.
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="space-y-4">
          <Card>
            <CardContent className="flex flex-wrap items-center gap-5 p-5">
              <ScoreRing score={job.score} size={64} />
              <div className="min-w-0 flex-1">
                <div className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Соответствие</div>
                <div className="text-[20px] font-semibold leading-tight">{job.score >= 80 ? "Высокое" : job.score >= 65 ? "Хорошее" : "Среднее"}</div>
                <div className="mt-1 flex flex-wrap items-center gap-2">
                  <Badge variant={job.eligibility === "rejected" ? "danger" : "success"}>{jobVerdict(job).label}</Badge>
                  <span className="text-[12px] text-muted-foreground">Навыки {Math.round(s.skill_match || 0)} · Карьера {Math.round(s.career_fit || 0)}</span>
                </div>
                <details className="mt-2 text-[11.5px] text-muted-foreground">
                  <summary className="cursor-pointer hover:text-foreground">Подробнее об оценке</summary>
                  <div className="mt-1">{job.realism?.positive?.length ? `Плюсы: ${job.realism.positive.join(" · ")}. ` : ""}Диапазон {Math.round(job.score_low)}–{Math.round(job.score_high)}. Это соответствие требованиям, не вероятность приглашения.</div>
                </details>
              </div>
              {job.personal_probability?.available && (
                <div className="rounded-lg border px-3 py-2 text-[12px]">
                  <span className="text-muted-foreground">Персональный прогноз</span>
                  <div className="text-lg font-semibold">{Math.round((job.personal_probability.personal_probability || 0) * 100)}%</div>
                  <div className="text-[10.5px] text-muted-foreground">n={job.personal_probability.n} · {job.personal_probability.confidence_label}</div>
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader><CardTitle>Требования и совпадения</CardTitle></CardHeader>
            <CardContent className="space-y-3 text-[13px]">
              <div><div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Подтверждённые навыки</div>
                <div className="flex flex-wrap gap-1.5">{job.skills.matched.length ? job.skills.matched.map((x) => <Badge key={x} variant="success">{x}</Badge>) : <span className="text-muted-foreground">Нет совпадений</span>}</div></div>
              <div><div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Обязательные пробелы</div>
                <div className="flex flex-wrap gap-1.5">{job.skills.required_missing.length ? job.skills.required_missing.map((x) => <Badge key={x} variant="danger">{x}</Badge>) : <Badge variant="success">Критичных пробелов нет</Badge>}</div></div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="rounded-md border p-3"><div className="text-[11px] text-muted-foreground">Опыт</div><div className="font-medium">{job.experience.required_years === null || job.experience.required_years === undefined ? "не указан" : `${job.experience.required_years} г.`} / у вас {job.experience.candidate_years ?? 0} г.</div></div>
                <div className="rounded-md border p-3"><div className="text-[11px] text-muted-foreground">Образование</div><div className="font-medium">{job.education.required ? (job.education.confirmed ? "подтверждено" : "нужна проверка") : "не требуется"}</div></div>
              </div>
              {job.red_flags.length > 0 && <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-amber-800 dark:text-amber-200">Флаги: {job.red_flags.join(", ")}</div>}
            </CardContent>
          </Card>

          <details className="rounded-xl border bg-card p-4">
            <summary className="cursor-pointer text-[12.5px] font-medium text-muted-foreground hover:text-foreground">Детали оценки соответствия</summary>
            <div className="mt-3 space-y-2.5">
              {factors.length ? factors.map((f) => (
                <div key={f.key}>
                  <div className="mb-1 flex items-center justify-between text-[12px]">
                    <span className="text-muted-foreground">{FACTOR_LABELS[f.key] || f.key} <span className="opacity-60">· вес {Math.round(f.weight * 100)}%</span></span>
                    <b className="tabular-nums">{Math.round(f.value * 100)}</b>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                    <div className={f.value >= 0.65 ? "h-full rounded-full bg-emerald-500/80" : f.value >= 0.35 ? "h-full rounded-full bg-primary/80" : "h-full rounded-full bg-amber-500/80"} style={{ width: `${Math.max(2, Math.round(f.value * 100))}%` }} />
                  </div>
                </div>
              )) : <div className="text-[12.5px] text-muted-foreground">Детализация появится после пересчёта вакансии.</div>}
            </div>
          </details>

          <Card>
            <Tabs defaultValue="letter">
              <CardHeader className="flex-col items-stretch gap-2 pb-0 sm:flex-row sm:items-center sm:justify-between">
                <TabsList><TabsTrigger value="letter">Письмо</TabsTrigger><TabsTrigger value="description">Описание</TabsTrigger><TabsTrigger value="history">История</TabsTrigger></TabsList>
                <Button size="sm" variant="outline" className="w-full sm:w-auto" onClick={() => setGenOpen(true)} disabled={busy === "gen"}>
                  {busy === "gen" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}Сгенерировать
                </Button>
              </CardHeader>
              <CardContent className="pt-3">
                <TabsContent value="letter" className="mt-0">
                  <Textarea aria-label="Сопроводительное письмо" value={letter} onChange={(e) => setLetter(e.target.value)} className="min-h-[220px] font-[13px] leading-relaxed" placeholder="Сопроводительное письмо" />
                  <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
                    <span className="text-[11.5px] text-muted-foreground">
                      Источник: {letterOrigin(job.letter_source) || job.letter_source || "нет"} ·{" "}
                      {!job.letter ? "факт-чек: —" : (job.letter_checks?.fallback_reason
                        ? `факт-чек: безопасный шаблон (${job.letter_checks.fallback_reason.slice(0, 80)})`
                        : (job.letter_checks?.critique?.unsupported_claims?.length
                          ? "факт-чек: есть замечания"
                          : "факт-чек: пройден"))}
                      {" · проверьте факты"}
                    </span>
                    <Button size="sm" onClick={() => action("save", async () => { await apiPost("/api/jobs/letter", { id: job.id, text: letter }); })} disabled={busy === "save"}>Сохранить письмо</Button>
                  </div>
                </TabsContent>
                <TabsContent value="description" className="mt-0 max-h-[420px] overflow-auto whitespace-pre-wrap text-[13px] leading-relaxed text-muted-foreground">{job.description || "Описание не загружено"}</TabsContent>
                <TabsContent value="history" className="mt-0 space-y-2">
                  {job.applications.length ? job.applications.map((a) => (
                    <div key={a.id} className="rounded-md border p-3 text-[12.5px]"><b>{QUEUE_LABELS[a.state] || a.state}</b> · {a.channel} · {dt(a.sent_at || a.updated_at)}{a.last_error ? <div className="text-destructive">{a.last_error}</div> : null}</div>
                  )) : <div className="text-[12.5px] text-muted-foreground">Отклик ещё не создан.</div>}
                  {job.events.slice(0, 8).map((e) => <div key={e.id} className="text-[11.5px] text-muted-foreground">{dt(e.created_at)} · {e.type}</div>)}
                </TabsContent>
              </CardContent>
            </Tabs>
          </Card>
        </div>

        <div className="space-y-4">
          <Card>
            <CardHeader><CardTitle>Действия</CardTitle></CardHeader>
            <CardContent className="space-y-2">
              <Button className="w-full" onClick={() => {
                if (!job.eligible && !window.confirm("Позиция не прошла автоматический отбор. Подготовить письмо для ручной проверки? Это может стоить денег через AITUNNEL. Автоотправки не будет.")) return;
                action("prep", async () => { await apiPost("/api/jobs/prepare", {
                  id: job.id, variant: job.variant, manual_review: !job.eligible,
                }); });
              }} disabled={busy === "prep" || !verdict.canPrepare} title={verdict.hint}>
                {busy === "prep" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}{job.eligible ? "Подготовить отклик" : "Подготовить для проверки"}
              </Button>
              {!verdict.canPrepare && <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-2 text-[11.5px] text-amber-800 dark:text-amber-200">{verdict.hint}</div>}
              <Button className="w-full" variant="outline" asChild><a href={job.resume_preview_url} target="_blank" rel="noopener noreferrer"><FileText className="h-4 w-4" />Резюме под вакансию</a></Button>
              <div className="text-[11.5px] text-muted-foreground">Резюме: {job.variant_label}. Автоотправка не выполняется.</div>
              {saved && <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 p-2 text-[12px] text-emerald-700 dark:text-emerald-300">{saved}</div>}
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>Параметры</CardTitle></CardHeader>
            <CardContent className="space-y-2 text-[12.5px]">
              <div className="flex justify-between"><span className="text-muted-foreground">Источник</span><span>{job.source}</span></div>
              <div className="flex justify-between"><span className="text-muted-foreground">Опубликовано</span><span>{dt(job.published_at)}</span></div>
              <div className="flex justify-between"><span className="text-muted-foreground">Найдено</span><span>{dt(job.fetched_at)}</span></div>
              <div className="flex justify-between"><span className="text-muted-foreground">Уровень</span><span>{job.seniority}</span></div>
            </CardContent>
          </Card>
          {(job.publications || []).length > 1 && (
            <Card>
              <CardHeader><CardTitle>Публикации этой вакансии</CardTitle></CardHeader>
              <CardContent className="space-y-2">
                {(job.publications || []).map((p) => (
                  <a key={p.copy_id} href={p.url} target="_blank" rel="noopener noreferrer"
                     className="block rounded-md border px-2.5 py-2 text-[12px] hover:border-primary/40 hover:bg-accent">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium">{SOURCE_LABELS[p.source] || p.source}</span>
                      {p.copy_id === p.canonical_id
                        ? <Badge variant="primary">основная</Badge>
                        : <Badge>{Math.round(p.similarity * 100)}%</Badge>}
                    </div>
                    <div className="mt-0.5 line-clamp-1 text-muted-foreground">{p.title}</div>
                  </a>
                ))}
                <div className="text-[11px] text-muted-foreground">
                  Это одна и та же вакансия на разных площадках. Ссылки на все публикации сохранены.
                </div>
              </CardContent>
            </Card>
          )}
          <Card>
            <CardHeader className="flex-row items-center justify-between gap-2">
              <CardTitle>Источники и подробности</CardTitle>
              <Button size="sm" variant="outline" onClick={enrich} disabled={enriching}>
                {enriching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                Обновить
              </Button>
            </CardHeader>
            <CardContent className="space-y-2 text-[12px]">
              {job.source_post_url && (
                <a href={job.source_post_url} target="_blank" rel="noopener noreferrer"
                   className="block truncate text-primary hover:underline">Telegram-пост</a>
              )}
              {job.apply_url && (
                <a href={job.apply_url} target="_blank" rel="noopener noreferrer"
                   className="block truncate text-primary hover:underline">Официальная страница вакансии</a>
              )}
              {job.enriched_at && <div className="text-muted-foreground">Обновлено: {dt(job.enriched_at)}</div>}
              {job.enrichment?.fields?.valid_through?.value && (
                <div className="text-muted-foreground">
                  Подать до: {String(job.enrichment.fields.valid_through.value).slice(0, 10)}
                </div>
              )}
              {job.enrichment?.missing?.length ? (
                <div className="text-amber-700 dark:text-amber-300">
                  Не указано: {job.enrichment.missing.map((x: string) => ({
                    company: "компания", salary_min: "зарплата", location: "город",
                    experience_years: "опыт",
                  } as Record<string, string>)[x] || x).join(", ")}
                </div>
              ) : null}
              {job.enrichment?.links?.length ? (
                <details>
                  <summary className="cursor-pointer text-muted-foreground">Ссылки из поста</summary>
                  <div className="mt-1 space-y-1">
                    {job.enrichment.links.slice(0, 5).map((l: any) => (
                      <a key={l.url} href={l.url} target="_blank" rel="noopener noreferrer"
                         className="block truncate text-[11.5px] text-muted-foreground hover:text-foreground">
                        {l.kind}: {l.host}
                      </a>
                    ))}
                  </div>
                </details>
              ) : null}
              {!job.enrichment?.fetched_at && <div className="text-muted-foreground">Подробности ещё не загружены.</div>}
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>Переписка</CardTitle></CardHeader>
            <CardContent className="space-y-2">
              {job.messages.length ? job.messages.slice(0, 4).map((m) => (
                <div key={m.id} className="rounded-md border p-2 text-[12px]"><div className="mb-1 flex justify-between"><Badge>{m.category}</Badge><span className="text-muted-foreground">{dt(m.received_at)}</span></div>{m.body}</div>
              )) : <div className="text-[12.5px] text-muted-foreground">Сообщений нет. Импортируйте ответ на странице откликов.</div>}
            </CardContent>
          </Card>
        </div>
      </div>

      <Dialog open={genOpen} onOpenChange={setGenOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Сгенерировать письмо?</DialogTitle>
            <DialogDescription>
              Письмо создаётся языковой моделью по фактам резюме и проходит проверку на выдуманные данные.
              Запрос может быть платным — ориентир 0,2–1 ₽, точная сумма попадёт в ledger.
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setGenOpen(false)}>Отмена</Button>
            <Button
              onClick={() => { setGenOpen(false); action("gen", async () => { await apiPost("/api/jobs/generate-letter", { id: job.id }); }); }}
            >
              <Wand2 className="h-4 w-4" />Сгенерировать
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
