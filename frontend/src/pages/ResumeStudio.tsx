import { useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2, Download, FileUp, Loader2, RefreshCw, Sparkles, Upload, Wand2, XCircle,
} from "lucide-react";
import { apiGet, apiPost, apiUpload } from "@/lib/api";
import { OnboardingData, ResumeData } from "@/lib/types";
import { PageHeader, ErrorState, LoadingState } from "@/components/PageChrome";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Select, Textarea } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Progress } from "@/components/ui/progress";

export default function ResumePage() {
  const [resume, setResume] = useState<ResumeData | null>(null);
  const [onb, setOnb] = useState<OnboardingData | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [checkedSkills, setCheckedSkills] = useState<number[]>([]);
  const [selected, setSelected] = useState<Record<string, any[]>>({});
  const [form, setForm] = useState<any>({});
  const fileRef = useRef<HTMLInputElement>(null);

  async function load() {
    try {
      const [r, o] = await Promise.all([apiGet<ResumeData>("/api/resume"), apiGet<OnboardingData>("/api/onboarding")]);
      setResume(r); setOnb(o);
      setCheckedSkills((r.resume.skills || []).map((s: any, i: number) => (s.verified ? i : -1)).filter((i: number) => i >= 0));
      if (o.draft) {
        const d = o.draft;
        const all = (arr: any[]) => (arr || []).map((_, i) => i);
        setSelected({ skills: all(d.skills), projects: all(d.projects), education: all(d.education), experience: all(d.experience), languages: all(d.languages), directions: (d.suggestions?.directions || []).map((x: string) => x) as any });
        setForm({
          headline: d.headline || "", summary: d.summary || "", email: d.contact?.email || "", phone: d.contact?.phone || "",
          city: d.contact?.city || "", locations: (d.suggestions?.constraints?.locations || []).join(", "),
          salary_target_rub: d.suggestions?.constraints?.salary_target_rub || "",
          availability: d.suggestions?.constraints?.availability || "",
          mode_remote: (d.suggestions?.constraints?.work_modes || []).includes("удалённо"),
          mode_hybrid: (d.suggestions?.constraints?.work_modes || []).includes("гибрид"),
          mode_office: (d.suggestions?.constraints?.work_modes || []).includes("офис"),
        });
      }
      setError("");
    } catch (e) { setError((e as Error).message); }
  }
  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (!onb?.runner.running) return;
    const id = setInterval(load, 2500);
    return () => clearInterval(id);
  }, [onb?.runner.running]);

  async function run(name: string, fn: () => Promise<any>) {
    setBusy(name); setError("");
    try { await fn(); await load(); } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function upload(file: File) {
    await run("upload", async () => { await apiUpload("/api/onboarding/upload", file); });
  }

  async function confirmProfile(start: boolean) {
    const d = onb?.draft;
    if (!d) return;
    await run("confirm", async () => {
      await apiPost("/api/onboarding/confirm", {
        ...form,
        skills: (selected.skills || []).map(String),
        projects: (selected.projects || []).map(String),
        education: (selected.education || []).map(String),
        experience: (selected.experience || []).map(String),
        languages: (selected.languages || []).map(String),
        directions: selected.directions || [],
        start,
      });
    });
  }

  if (error && !resume) return <ErrorState error={error} onRetry={load} />;
  if (!resume) return <LoadingState rows={4} />;
  const c = resume.completeness;
  const draft = onb?.draft;
  const total = 4;
  const done = [c.skills >= 3, c.projects >= 1, c.education >= 1, c.contact].filter(Boolean).length;

  const toggle = (section: string, index: number) => setSelected((s) => {
    const current = new Set<number>(s[section] || []);
    current.has(index) ? current.delete(index) : current.add(index);
    return { ...s, [section]: Array.from(current).sort((a, b) => a - b) };
  });

  return (
    <div className="animate-rise">
      <PageHeader eyebrow="Резюме" title="Resume Studio"
        description="Единая база подтверждённых фактов. Импорт, разбор Qwen/AITUNNEL, проверка и версии."
        actions={<Button variant="outline" size="sm" onClick={() => fileRef.current?.click()} disabled={!!busy}><FileUp className="h-3.5 w-3.5" />Загрузить резюме</Button>} />
      <input ref={fileRef} type="file" accept=".pdf,.docx,.txt,.md" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); }} />
      {error && <div className="mb-3 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-[12px] text-destructive">{error}</div>}

      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Card className="p-4"><div className="text-[11px] text-muted-foreground">Подтверждённые навыки</div><div className="text-xl font-semibold">{c.skills}</div>{draft ? <div className="text-[11px] text-muted-foreground">в черновике: {(draft.skills || []).length}</div> : null}</Card>
        <Card className="p-4"><div className="text-[11px] text-muted-foreground">Подтверждённые проекты</div><div className="text-xl font-semibold">{c.projects}</div>{draft ? <div className="text-[11px] text-muted-foreground">в черновике: {(draft.projects || []).length}</div> : null}</Card>
        <Card className="p-4"><div className="text-[11px] text-muted-foreground">Образование</div><div className="text-xl font-semibold">{c.education}</div>{draft ? <div className="text-[11px] text-muted-foreground">в черновике: {(draft.education || []).length}</div> : null}</Card>
        <Card className="p-4"><div className="text-[11px] text-muted-foreground">Контакты</div><div className="text-xl font-semibold">{c.contact ? "есть" : "нет"}</div><div className="text-[11px] text-muted-foreground">для писем и резюме</div></Card>
      </div>
      <div className="mb-4"><div className="mb-1 flex justify-between text-[12px] text-muted-foreground"><span>Заполненность профиля</span><span>{done}/{total}</span></div><Progress value={(done / total) * 100} /></div>

      <Tabs defaultValue={draft?.status === "review" ? "draft" : "facts"}>
        <TabsList><TabsTrigger value="draft">Черновик и разбор</TabsTrigger><TabsTrigger value="facts">Подтверждённые факты</TabsTrigger><TabsTrigger value="versions">Версии и экспорт</TabsTrigger></TabsList>
        <TabsContent value="draft">
          {!draft ? (
            <Card className="p-8 text-center">
              <Upload className="mx-auto mb-3 h-7 w-7 text-muted-foreground" />
              <div className="text-[15px] font-semibold">Загрузите резюме</div>
              <div className="mx-auto mt-1 max-w-md text-[13px] text-muted-foreground">PDF, DOCX, TXT или MD. Разбор локальный, через Qwen или AITUNNEL. Ничего не сохраняется без подтверждения.</div>
              <Button className="mt-4" onClick={() => fileRef.current?.click()}><Upload className="h-4 w-4" />Выбрать файл</Button>
              {onb?.files?.length ? <div className="mt-4 text-[12px] text-muted-foreground">Уже загружены: {onb.files.slice(0, 4).join(", ")}</div> : null}
            </Card>
          ) : (
            <div className="space-y-4">
              <Card>
                <CardContent className="flex flex-wrap items-center gap-2 p-4">
                  <Badge variant="primary">{draft.method}</Badge>
                  <Badge>{draft.source_name}</Badge>
                  <span className="text-[12px] text-muted-foreground">{draft.status === "review" ? "Ожидает подтверждения" : "Подтверждён"}</span>
                  <div className="flex-1" />
                  <Button size="sm" variant="outline" onClick={() => run("qwen", () => apiPost("/api/onboarding/qwen"))} disabled={!!busy || !onb?.qwen.available} title={onb?.qwen.reason}>
                    {onb?.runner.running && onb.runner.kind === "qwen" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}Qwen
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => run("ai", () => apiPost("/api/onboarding/aitunnel"))} disabled={!!busy || !onb?.aitunnel.available}>AITUNNEL</Button>
                  {onb?.runner.running && <Button size="sm" variant="ghost" onClick={() => run("cancel", () => apiPost("/api/onboarding/cancel"))}>Отменить</Button>}
                  <Button size="sm" variant="ghost" onClick={() => run("discard", () => apiPost("/api/onboarding/discard"))}><XCircle className="h-3.5 w-3.5" />Удалить</Button>
                </CardContent>
                {(onb?.runner.running || onb?.runner.error) && <div className="border-t px-4 py-2 text-[12px] text-muted-foreground">{onb.runner.running ? `Разбор ${onb.runner.kind}: ${onb.runner.elapsed} с…` : `Ошибка: ${onb.runner.error}`}</div>}
              </Card>

              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                <Card><CardHeader><CardTitle>Контакты и заголовок</CardTitle></CardHeader><CardContent className="space-y-2">
                  <Input aria-label="Имя и роль" placeholder="Имя и роль" value={form.headline || ""} onChange={(e) => setForm({ ...form, headline: e.target.value })} />
                  <div className="grid grid-cols-2 gap-2"><Input aria-label="Email" placeholder="Email" value={form.email || ""} onChange={(e) => setForm({ ...form, email: e.target.value })} /><Input aria-label="Телефон" placeholder="Телефон" value={form.phone || ""} onChange={(e) => setForm({ ...form, phone: e.target.value })} /></div>
                  <Input aria-label="Город" placeholder="Город" value={form.city || ""} onChange={(e) => setForm({ ...form, city: e.target.value })} />
                  <Textarea aria-label="О себе" placeholder="О себе" value={form.summary || ""} onChange={(e) => setForm({ ...form, summary: e.target.value })} />
                </CardContent></Card>
                <Card><CardHeader><CardTitle>Предположения</CardTitle></CardHeader><CardContent className="space-y-2">
                  <div className="text-[11px] text-muted-foreground">Направления</div>
                  <div className="flex flex-wrap gap-2">{(draft.suggestions?.directions || []).map((x: string) => (
                    <label key={x} className="flex items-center gap-1.5 rounded-md border px-2 py-1 text-[12px]"><input type="checkbox" checked={(selected.directions || []).includes(x)} onChange={() => setSelected((s) => ({ ...s, directions: (s.directions || []).includes(x) ? (s.directions || []).filter((d) => d !== x) : [...(s.directions || []), x] }))} />{x}</label>
                  ))}</div>
                  <div className="flex flex-wrap gap-3 pt-1 text-[12px]">
                    <label className="flex items-center gap-1.5"><input type="checkbox" checked={!!form.mode_remote} onChange={(e) => setForm({ ...form, mode_remote: e.target.checked })} />удалённо</label>
                    <label className="flex items-center gap-1.5"><input type="checkbox" checked={!!form.mode_hybrid} onChange={(e) => setForm({ ...form, mode_hybrid: e.target.checked })} />гибрид</label>
                    <label className="flex items-center gap-1.5"><input type="checkbox" checked={!!form.mode_office} onChange={(e) => setForm({ ...form, mode_office: e.target.checked })} />офис</label>
                  </div>
                  <Input aria-label="Города" placeholder="Города" value={form.locations || ""} onChange={(e) => setForm({ ...form, locations: e.target.value })} />
                  <Input aria-label="Зарплата, ₽" placeholder="Зарплата, ₽" value={form.salary_target_rub || ""} onChange={(e) => setForm({ ...form, salary_target_rub: e.target.value })} />
                  <Input aria-label="Доступность" placeholder="Доступность" value={form.availability || ""} onChange={(e) => setForm({ ...form, availability: e.target.value })} />
                </CardContent></Card>
              </div>

              <Card><CardHeader><CardTitle>Факты из резюме — подтвердите выбранное</CardTitle></CardHeader><CardContent className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                <div>
                  <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Навыки</div>
                  <div className="grid grid-cols-2 gap-1.5">{(draft.skills || []).map((s: any, i: number) => (
                    <label key={i} className="flex items-start gap-1.5 text-[12px]"><input type="checkbox" checked={(selected.skills || []).includes(i)} onChange={() => toggle("skills", i)} /><span>{s.name}<div className="text-[10px] text-muted-foreground">{s.evidence}</div></span></label>
                  ))}</div>
                </div>
                <div>
                  <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Проекты</div>
                  {(draft.projects || []).map((p: any, i: number) => (
                    <label key={i} className="mb-2 flex items-start gap-1.5 text-[12px]"><input type="checkbox" checked={(selected.projects || []).includes(i)} onChange={() => toggle("projects", i)} /><span><b>{p.name}</b><div className="text-[11px] text-muted-foreground">{p.description?.slice(0, 220)}</div></span></label>
                  ))}
                  <div className="mb-2 mt-3 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Образование</div>
                  {(draft.education || []).map((e: any, i: number) => (
                    <label key={i} className="flex items-start gap-1.5 text-[12px]"><input type="checkbox" checked={(selected.education || []).includes(i)} onChange={() => toggle("education", i)} /><span><b>{e.institution}</b><div className="text-[11px] text-muted-foreground">{e.field} {e.start}–{e.end}</div></span></label>
                  ))}
                  <div className="mb-2 mt-3 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Опыт</div>
                  {(draft.experience || []).length ? draft.experience.map((e: any, i: number) => (
                    <label key={i} className="flex items-start gap-1.5 text-[12px]"><input type="checkbox" checked={(selected.experience || []).includes(i)} onChange={() => toggle("experience", i)} /><span>{e.company} {e.position}</span></label>
                  )) : <div className="text-[11.5px] text-muted-foreground">В резюме опыт не указан — не выдумываем.</div>}
                </div>
              </CardContent></Card>
              <div className="flex flex-wrap gap-2">
                {draft.status === "confirmed" ? (
                  <div className="w-full rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-[12.5px] text-emerald-700 dark:text-emerald-300">
                    Профиль подтверждён — факты записаны в SQLite и используются в скоринге. Чтобы изменить состав, загрузите новое резюме.
                  </div>
                ) : (
                  <>
                    <Button onClick={() => confirmProfile(true)} disabled={!!busy}><CheckCircle2 className="h-4 w-4" />Подтвердить и запустить поиск</Button>
                    <Button variant="outline" onClick={() => confirmProfile(false)} disabled={!!busy}>Подтвердить без поиска</Button>
                  </>
                )}
              </div>
            </div>
          )}
        </TabsContent>

        <TabsContent value="facts">
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Card><CardHeader><CardTitle>Навыки</CardTitle></CardHeader><CardContent className="space-y-1.5">
              {(resume.resume.skills || []).map((s: any, i: number) => (
                <label key={i} className="flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-[12.5px]">
                  <input type="checkbox" checked={checkedSkills.includes(i)} onChange={() => setCheckedSkills((x) => x.includes(i) ? x.filter((v) => v !== i) : [...x, i])} />
                  <span className="flex-1">{s.name}</span>
                  {s.verified ? <Badge variant="success">подтверждён</Badge> : <Badge variant="warning">не подтверждён</Badge>}
                </label>
              ))}
              <Button size="sm" className="mt-2" onClick={() => run("confirm", () => apiPost("/api/resume/confirm", { indexes: checkedSkills }))}>Сохранить подтверждения</Button>
              {!(resume.resume.skills || []).length && <div className="rounded-md border border-dashed p-3 text-[12px] text-muted-foreground">Подтверждённых навыков пока нет. Подтвердите черновик на вкладке «Черновик и разбор».</div>}
            </CardContent></Card>
            <Card><CardHeader><CardTitle>Проекты</CardTitle></CardHeader><CardContent className="space-y-2">
              {(resume.resume.projects || []).map((p: any, i: number) => (
                <div key={i} className="rounded-md border p-2.5 text-[12.5px]"><div className="flex items-center justify-between gap-2"><b>{p.name}</b>{p.verified ? <Badge variant="success">подтверждён</Badge> : <Badge variant="warning">не подтверждён</Badge>}</div><div className="text-[11px] text-muted-foreground">{p.description}</div><Button size="sm" variant="outline" className="mt-1" onClick={() => run("project", () => apiPost("/api/resume/project", { index: i }))}>{p.verified ? "Снять" : "Подтвердить"}</Button></div>
              ))}
              {(resume.resume.education || []).map((e: any, i: number) => <div key={i} className="rounded-md border p-2.5 text-[12.5px]"><b>{e.institution}</b><div className="text-[11px] text-muted-foreground">{e.field} {e.start}–{e.end}</div></div>)}
              {!(resume.resume.projects || []).length && !(resume.resume.education || []).length && (
                <div className="rounded-md border border-dashed p-3 text-[12px] text-muted-foreground">Проекты и образование появятся после подтверждения фактов из резюме.</div>
              )}
            </CardContent></Card>
          </div>
        </TabsContent>

        <TabsContent value="versions">
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            {Object.entries(resume.variants).map(([key, v]) => (
              <Card key={key}><CardHeader><CardTitle>{v.label}</CardTitle></CardHeader><CardContent className="space-y-2">
                <div className="text-[12px] text-muted-foreground">{v.skills} навыков · {v.projects} проектов</div>
                <div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => run("export", () => apiPost("/api/resume/export", { variant: key }))}><Download className="h-3.5 w-3.5" />DOCX/PDF</Button></div>
              </CardContent></Card>
            ))}
          </div>
          <Card className="mt-4"><CardHeader><CardTitle>История версий</CardTitle></CardHeader><CardContent className="space-y-2">
            {resume.versions.length ? resume.versions.map((v) => <div key={v.name} className="flex items-center justify-between rounded-md border p-2 text-[12px]"><span>{v.created} · {v.size} байт</span><Button size="sm" variant="outline" onClick={() => run("restore", () => apiPost("/api/resume/restore", { name: v.name }))}>Восстановить</Button></div>) : <div className="text-[12px] text-muted-foreground">История появится после сохранения.</div>}
          </CardContent></Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
