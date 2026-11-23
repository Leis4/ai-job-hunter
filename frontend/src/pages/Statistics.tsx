import { useEffect, useState } from "react";
import { BarChart3, ChevronDown, Download, Loader2, RefreshCw, Sparkles, Upload } from "lucide-react";
import { apiGet, apiPost, apiUploadMany } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, LoadingState } from "@/components/PageChrome";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";

const LABELS: Record<string, string> = {
  invited: "Приглашения", offer: "Офферы", rejected: "Отказы", waiting: "Ожидают ответа",
  submitted: "Отправлены", viewed: "Просмотрены", interview: "Интервью", unknown: "Неизвестно",
  not_viewed: "Не просмотрены", responded: "Ответ без решения",
};

export default function StatisticsPage() {
  const [data, setData] = useState<any>(null);
  const [outcomes, setOutcomes] = useState<any[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [preview, setPreview] = useState<any>(null);

  async function load() {
    try {
      const [p, o] = await Promise.all([apiGet<any>("/api/probability"), apiGet<any>("/api/outcomes")]);
      setData(p); setOutcomes(o.items || []); setError("");
    } catch (e) { setError((e as Error).message); }
  }
  useEffect(() => { load(); }, []);

  async function run(label: string, fn: () => Promise<any>) {
    setBusy(label); setError(""); setNotice("");
    try { const r = await fn(); setNotice(typeof r === "string" ? r : "Готово."); await load(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  async function uploadFiles() {
    if (!files.length) return;
    await run("upload", async () => {
      const r = await apiUploadMany<any>("/api/outcomes/upload", files);
      setPreview(r); setFiles([]);
      return `Найдено откликов: ${r.total}. Проверьте и подтвердите импорт.`;
    });
  }
  async function commit() {
    if (!preview?.rows?.length) return;
    await run("commit", async () => {
      const r = await apiPost<any>("/api/outcomes/import", { source: preview.source, rows: preview.rows });
      setPreview(null);
      return `Импортировано: ${r.added} новых, ${r.updated} обновлено.`;
    });
  }
  async function captureHistory() {
    await run("capture", async () => {
      const r = await apiPost<any>("/api/hh/browser/capture-history", { pages: 5 });
      if (!r.ok) throw new Error(r.reason || "Страницы не сохранены");
      return `Сохранено файлов: ${r.pages}. Теперь выберите их ниже для импорта.`;
    });
  }

  if (error && !data) return <ErrorState error={error} onRetry={load} />;
  if (!data) return <LoadingState rows={4} />;
  const s = data.summary || {};
  const counts = s.counts || {};
  const total = s.total || 0;
  const pending = (counts.waiting || 0) + (counts.unknown || 0) + (counts.not_viewed || 0) + (counts.submitted || 0);
  const timeline = (s.timeline || []).slice(-14);
  const maxTimeline = Math.max(1, ...timeline.map((t: any) => Number(t.n) || 0));
  const train = data.quality?.trained;

  return (
    <div className="animate-rise">
      <PageHeader eyebrow="Аналитика" title="Статистика"
        description="Понятная картина результатов. Подробные метрики и импорт — на втором уровне."
        actions={<div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => run("refresh", load)}><RefreshCw className="h-3.5 w-3.5" />Обновить</Button><Button size="sm" asChild><a href="/api/export/analytics.json"><Download className="h-3.5 w-3.5" />Экспорт</a></Button></div>} />

      {notice && <div className="mb-3 rounded-xl border bg-card p-3 text-[12px] text-muted-foreground">{notice}</div>}
      {error && <div className="mb-3 rounded-xl border border-destructive/25 bg-destructive/5 p-3 text-[12px] text-destructive">{error}</div>}

      <div className="mb-4 grid grid-cols-3 gap-3">
        <Card className="p-4"><div className="text-[12px] text-muted-foreground">Приглашения</div><div className="mt-2 text-[26px] font-semibold">{s.successes || 0}</div><div className="mt-1 text-[11px] text-muted-foreground">подтверждённые</div></Card>
        <Card className="p-4"><div className="text-[12px] text-muted-foreground">Отказы</div><div className="mt-2 text-[26px] font-semibold">{s.failures || 0}</div><div className="mt-1 text-[11px] text-muted-foreground">подтверждённые</div></Card>
        <Card className="p-4"><div className="text-[12px] text-muted-foreground">Ожидают / неизвестно</div><div className="mt-2 text-[26px] font-semibold">{pending}</div><div className="mt-1 text-[11px] text-muted-foreground">не считаются отказом</div></Card>
      </div>

      {total === 0 ? (
        <Card className="p-6">
          <EmptyState icon={<BarChart3 className="h-5 w-5" />} title="Истории пока нет" description="Подключите hh.ru через браузер и сохраните страницы истории или загрузите HTML/ZIP." action={<Button size="sm" onClick={captureHistory} disabled={busy === "capture"}>{busy === "capture" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Upload className="h-3.5 w-3.5" />}Открыть историю hh.ru</Button>} />
        </Card>
      ) : (
        <Card className="mb-4 p-5">
          <div className="flex items-center justify-between"><h2 className="text-[14px] font-semibold">Динамика откликов</h2><span className="text-[11px] text-muted-foreground">{total} записей</span></div>
          <div className="mt-4 flex h-24 items-end gap-1.5">
            {timeline.length ? timeline.map((t: any, i: number) => <div key={i} title={`${t.month}: ${LABELS[t.status] || t.status} ${t.n}`} className="flex-1 rounded-t bg-primary/60" style={{ height: `${Math.max(6, (Number(t.n) / maxTimeline) * 100)}%` }} />) : <div className="w-full text-center text-[12px] text-muted-foreground">Недостаточно данных для графика.</div>}
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
            <div className="flex flex-wrap gap-x-3 gap-y-1">{Object.entries(counts).filter(([, n]) => Number(n) > 0).map(([k, n]) => <span key={k}>{LABELS[k] || k}: <b className="text-foreground">{String(n)}</b></span>)}</div>
            {timeline.length > 1 && <span>{String(timeline[0].month)} — {String(timeline[timeline.length - 1].month)}</span>}
          </div>
        </Card>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="p-4">
          <div className="flex items-center gap-2"><Sparkles className="h-4 w-4 text-primary" /><h2 className="text-[14px] font-semibold">Персональный прогноз</h2></div>
          <div className="mt-2 text-[13px] font-medium">{train ? "Прогноз доступен" : "Персональный прогноз пока недоступен"}</div>
          <p className="mt-1 text-[11.5px] text-muted-foreground">{train ? "Точность и охват — в подробной аналитике." : "Для расчёта нужна история приглашений и отказов. Это не ошибка и не сбой."}</p>
          <Button size="sm" variant="outline" className="mt-3" onClick={() => setAdvanced(true)}>Подробная аналитика</Button>
        </Card>
        <Card className="p-4">
          <div className="flex items-center justify-between"><div className="flex items-center gap-2"><Upload className="h-4 w-4 text-primary" /><h2 className="text-[14px] font-semibold">Импорт истории</h2></div><Badge variant="outline">локально</Badge></div>
          <p className="mt-1 text-[11.5px] text-muted-foreground">Страницы hh.ru или HTML/ZIP. Пароли и cookies не нужны.</p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <label className="inline-flex h-9 cursor-pointer items-center rounded-lg border bg-card px-3 text-[12px] font-medium transition-colors hover:bg-accent">
              {files.length ? `Выбрано файлов: ${files.length}` : "Выбрать HTML/ZIP"}
              <input type="file" multiple accept=".html,.htm,.zip,.json,.csv" className="sr-only" onChange={(e) => setFiles(Array.from(e.target.files || []))} />
            </label>
            <Button size="sm" onClick={uploadFiles} disabled={!files.length || busy === "upload"}>{busy === "upload" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}Предпросмотр</Button>
          </div>
          <button className="mt-2.5 text-[11.5px] font-medium text-primary hover:underline" onClick={captureHistory} disabled={busy === "capture"}>
            Открыть историю на hh.ru →
          </button>
          {preview && <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border p-2 text-[11.5px]"><span>Найдено: {preview.total}</span><Button size="sm" onClick={commit}>Подтвердить импорт</Button></div>}
        </Card>
      </div>

      <Dialog open={advanced} onOpenChange={setAdvanced}>
        <DialogContent className="max-h-[88vh] max-w-4xl overflow-auto">
          <DialogHeader><DialogTitle>Подробная аналитика</DialogTitle></DialogHeader>
          <div className="space-y-4 text-[12px]">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-lg border p-3"><b>По уровням</b>{(s.by_seniority || []).map((r: any) => <div key={r.seniority} className="mt-1 flex justify-between text-[11.5px] text-muted-foreground"><span>{r.seniority}</span><span>{r.n} · {r.successes} пригл. · {r.failures} отказов</span></div>) || <div className="text-muted-foreground">Нет данных</div>}</div>
              <div className="rounded-lg border p-3"><b>По направлениям</b>{(s.by_direction || []).map((r: any) => <div key={r.direction} className="mt-1 flex justify-between text-[11.5px] text-muted-foreground"><span>{r.direction}</span><span>{r.n} · {r.successes} пригл. · {r.failures} отказов</span></div>) || <div className="text-muted-foreground">Нет данных</div>}</div>
            </div>
            <div className="rounded-lg border p-3"><b>Качество модели</b><div className="mt-1 text-[11.5px] text-muted-foreground">{train ? `Модель: ${data.quality.active?.kind || "—"} · Brier ${data.quality.active?.metrics?.test?.brier ?? "—"} · N ${data.quality.active?.metrics?.test?.n ?? "—"}` : "ML выключен. Нужна подтверждённая история приглашений и отказов."}</div><div className="mt-1 text-[11px] text-muted-foreground">Данные: полных описаний {data.dataset?.complete_descriptions ?? 0}, частичных {data.dataset?.partial_descriptions ?? 0}, без описания {data.dataset?.missing_descriptions ?? 0}.</div></div>
            <div className="rounded-lg border p-3"><b>Последние исходы</b>{outcomes.length ? outcomes.slice(0, 12).map((o: any) => <div key={o.id} className="mt-1 flex flex-wrap justify-between gap-2 text-[11.5px] text-muted-foreground"><span>{o.title || "Без названия"} · {o.employer || "—"}</span><span>{LABELS[o.status] || o.status} · {String(o.sent_at || "").slice(0, 10)}</span></div>) : <div className="text-muted-foreground">Нет импортированных записей.</div>}</div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
