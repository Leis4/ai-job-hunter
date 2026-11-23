import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Activity, Cpu, Database, Download, KeyRound, RefreshCw, Save, Sparkles, Wallet } from "lucide-react";
import { apiGet, apiPost, apiUploadMany } from "@/lib/api";
import { SettingsData } from "@/lib/types";
import { dt } from "@/lib/utils";
import { PageHeader, ErrorState, LoadingState } from "@/components/PageChrome";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Select, Textarea } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export default function SettingsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = ["profile", "preferences", "search", "ai", "notifications", "diagnostics"].includes(searchParams.get("tab") || "")
    ? searchParams.get("tab")! : "profile";
  const [data, setData] = useState<SettingsData | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [profileForm, setProfileForm] = useState<any>(null);
  const [contactForm, setContactForm] = useState<any>({ first_name: "", last_name: "", email: "", phone: "" });
  const [memoryFacts, setMemoryFacts] = useState<any[]>([]);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [newFactKey, setNewFactKey] = useState("decision.");
  const [newFactValue, setNewFactValue] = useState("");
  const [importResult, setImportResult] = useState<any>(null);
  const [importSelected, setImportSelected] = useState<string[]>([]);
  const [skillText, setSkillText] = useState("");
  const [skillProposal, setSkillProposal] = useState<any>(null);
  const [learning, setLearning] = useState<any[]>([]);
  const [newLearningText, setNewLearningText] = useState("");
  const [resumePreview, setResumePreview] = useState<any>(null);
  const [projectForm, setProjectForm] = useState<any>({ name: "", stack: "", description: "" });
  const [settingsJson, setSettingsJson] = useState("");
  const [saved, setSaved] = useState("");
  const [tgChannels, setTgChannels] = useState<any[]>([]);
  const [tgCandidates, setTgCandidates] = useState<any[]>([]);
  const [tgSummary, setTgSummary] = useState<any>(null);
  const [tgSelected, setTgSelected] = useState<string[]>([]);
  const [tgInput, setTgInput] = useState("");
  const [tgBulk, setTgBulk] = useState("");
  const [tgBusy, setTgBusy] = useState("");
  const [hh, setHh] = useState<any>(null);
  const [autoCfg, setAutoCfg] = useState<any>(null);
  const [qwenKey, setQwenKey] = useState("");
  const [qwenTest, setQwenTest] = useState<any>(null);
  const [showQwenKey, setShowQwenKey] = useState(false);
  const [qwenForm, setQwenForm] = useState<any>({ monthly: "", letter: 1.5, daily: 30, fallback: true, cheap: "", quality: "" });
  const [submissionDomains, setSubmissionDomains] = useState("");
  const [emailForm, setEmailForm] = useState<any>({ smtp_host: "", smtp_port: 587, smtp_security: "starttls", smtp_username: "", from_email: "", from_name: "", password: "" });
  const [emailOpen, setEmailOpen] = useState(false);
  const [qwenKeyOpen, setQwenKeyOpen] = useState(false);
  const emailDirtyRef = useRef(false);

  async function load() {
    try {
      const d = await apiGet<SettingsData>("/api/settings");
      setData(d);
      setSettingsJson(JSON.stringify(d.settings, null, 2));
      setAutoCfg({ ...(d.settings.automation || {}), startups_hours: d.settings.startups?.hours ?? 24 });
      const cloudCfg = d.settings.qwen?.cloud || {};
      setQwenForm({
        monthly: d.settings.llm?.aitunnel?.monthly_budget_rub ?? "",
        letter: cloudCfg.max_letter_rub ?? 1.5,
        daily: cloudCfg.max_daily_rub ?? 30,
        fallback: cloudCfg.allow_fallback !== false,
        cheap: cloudCfg.models?.cheap || "qwen3.7-flash",
        quality: cloudCfg.models?.quality || "qwen3.8-flash",
      });
      const sc = d.settings.submission?.channels || {};
      setSubmissionDomains([...(sc.ats_form?.domains || []), ...(sc.career_site_form?.domains || [])]
        .filter((x: string, i: number, arr: string[]) => arr.indexOf(x) === i).join(", "));
      if (!emailDirtyRef.current) {
        setEmailForm({
          smtp_host: sc.email?.smtp_host || "",
          smtp_port: sc.email?.smtp_port || 587,
          smtp_security: sc.email?.smtp_security || "starttls",
          smtp_username: sc.email?.smtp_username || "",
          from_email: sc.email?.from_email || "",
          from_name: sc.email?.from_name || "",
          password: "",
        });
      }
      apiGet<any>("/api/telegram/channels").then((r) => setTgChannels(r.items || [])).catch(() => {});
      apiGet<any>("/api/telegram/candidates").then((r) => setTgCandidates(r.items || [])).catch(() => {});
      apiGet<any>("/api/telegram/summary").then(setTgSummary).catch(() => {});
      apiGet<any>("/api/hh/status").then(setHh).catch(() => {});
      const p = d.profile;
      setProfileForm({
        skills: (p.verified_skills || []).join(", "),
        roles: (p.preferences?.role_keywords || []).join(", "),
        locations: (p.preferences?.locations || []).join(", "),
        modes: (p.preferences?.work_modes || []).join(", "),
        salary: p.preferences?.salary_target_rub || "",
        excluded: (p.preferences?.excluded_titles || []).join(", "),
        availability: p.preferences?.availability || "",
        employment: (p.preferences?.employment || []).join(", "),
        schedule: p.preferences?.schedule || "",
        business_trips: !!p.preferences?.business_trips,
        relocation: !!p.preferences?.relocation,
      });
      apiGet<any>("/api/resume").then((r) => setContactForm({
        first_name: r.resume?.contact?.first_name || "",
        last_name: r.resume?.contact?.last_name || "",
        email: r.resume?.contact?.email || "",
        phone: r.resume?.contact?.phone || "",
      })).catch(() => {});
      apiGet<any>("/api/memory/facts?limit=80").then((r) => setMemoryFacts(r.items || [])).catch(() => {});
      apiGet<any>("/api/memory/learning").then((r) => setLearning(r.items || [])).catch(() => {});
      setError("");
    } catch (e) { setError((e as Error).message); }
  }
  useEffect(() => { load(); }, []);

  async function run(name: string, fn: () => Promise<any>, message = "Сохранено") {
    setBusy(name); setSaved(""); setError("");
    try { await fn(); setSaved(message); await load(); } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  function saveContact() {
    const missing = [["first_name", "имя"], ["last_name", "фамилия"], ["email", "email"]]
      .filter(([key]) => !String(contactForm[key] || "").trim());
    if (missing.length) {
      setError("Для отправки заявок заполните: " + missing.map(([, label]) => label).join(", "));
      return;
    }
    run("contact", () => apiPost("/api/resume/contact", contactForm), "Контактные данные сохранены");
  }

  function syncMemory() {
    run("memory-sync", () => apiPost("/api/memory/sync", { embed: true }), "Память обновлена");
  }

  async function forgetFact(key: string) {
    if (!window.confirm("Удалить этот подтверждённый факт из памяти?")) return;
    await run(`forget-${key}`, async () => {
      await apiPost("/api/memory/fact", { key, delete: true });
    }, "Факт удалён");
  }

  async function forgetSecret(name: string) {
    if (!window.confirm("Отключить этот интеграционный секрет? Его потребуется ввести заново.")) return;
    await run(`secret-${name}`, async () => {
      await apiPost("/api/memory/secret/forget", { name });
    }, "Секрет удалён");
  }

  async function addMemoryFact() {
    if (!newFactKey.trim() || !newFactValue.trim()) return;
    await run("memory-add", async () => {
      await apiPost("/api/memory/fact", {
        key: newFactKey.trim(), value: newFactValue.trim(),
        source: "user", kind: newFactKey.trim().startsWith("decision.") ? "decision" : "fact",
        index: true,
      });
      setNewFactValue("");
    }, "Факт сохранён в памяти");
  }

  async function importResume(file: File) {
    setBusy("resume-import"); setError(""); setSaved(""); setImportResult(null);
    try {
      const result = await apiUploadMany<any>("/api/memory/resume/import", [file]);
      setImportResult(result);
      setImportSelected((result.items || []).map((x: any) => x.key));
      setSaved(`Файл разобран: новых или изменённых фактов ${result.items?.length || 0}`);
      await load();
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function confirmImport() {
    if (!importResult?.import_id) return;
    await run("resume-confirm", async () => {
      const result = await apiPost<any>("/api/memory/resume/confirm", {
        import_id: importResult.import_id, selected: importSelected,
      });
      setImportResult(null);
      setImportSelected([]);
      setSaved(`Импорт применён: ${result.applied || 0} фактов`);
    }, "Резюме импортировано в память");
  }

  async function proposeSkill() {
    if (!skillText.trim()) return;
    setBusy("skill-propose"); setError("");
    try {
      const result = await apiPost<any>("/api/memory/skill", { text: skillText.trim() });
      setSkillProposal(result);
      if (!result.ok) throw new Error(result.reason || "Не удалось определить навык");
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function applySkill() {
    if (!skillProposal?.skill) return;
    await run("skill-apply", async () => {
      await apiPost("/api/memory/skill", {
        confirm: true, skill: skillProposal.skill, status: skillProposal.status,
        evidence: skillProposal.evidence || skillText,
      });
      setSkillText(""); setSkillProposal(null);
    }, "Навык обновлён");
  }

  async function addProject() {
    if (!projectForm.name.trim()) return;
    await run("project-add", async () => {
      await apiPost("/api/memory/project", {
        name: projectForm.name.trim(),
        stack: projectForm.stack.split(",").map((x: string) => x.trim()).filter(Boolean),
        description: projectForm.description.trim(),
      });
      setProjectForm({ name: "", stack: "", description: "" });
    }, "Проект добавлен");
  }

  async function previewResumeUpdate() {
    setBusy("resume-preview"); setError("");
    try {
      setResumePreview(await apiPost<any>("/api/memory/resume/preview", {}));
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function applyResumeUpdate() {
    await run("resume-update", async () => {
      const result = await apiPost<any>("/api/memory/resume/update", { confirm: true });
      setResumePreview(null);
      setSaved(`Резюме обновлено из памяти: ${result.skills || 0} навыков, ${result.projects || 0} проектов`);
    }, "Резюме обновлено из памяти");
  }

  async function addLearningEntry() {
    if (!newLearningText.trim()) return;
    await run("learning-add", async () => {
      await apiPost("/api/memory/learning", { description: newLearningText.trim(), skills: [] });
      setNewLearningText("");
    }, "Запись обучения сохранена");
  }

  function saveProfile() {    if (!data || !profileForm) return;
    const split = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);
    const profile = {
      ...data.profile,
      verified_skills: split(profileForm.skills),
      preferences: {
        ...(data.profile.preferences || {}),
        role_keywords: split(profileForm.roles),
        locations: split(profileForm.locations),
        work_modes: split(profileForm.modes),
        salary_target_rub: profileForm.salary ? Number(profileForm.salary) : null,
        excluded_titles: split(profileForm.excluded),
        availability: profileForm.availability,
        employment: split(profileForm.employment),
        schedule: profileForm.schedule,
        business_trips: !!profileForm.business_trips,
        relocation: !!profileForm.relocation,
      },
    };
    run("profile", () => apiPost("/api/profile", { profile }), "Профиль сохранён");
  }

  async function tgAction(action: string, channel = "", enabled = true) {
    setBusy("tg-" + action + channel);
    setError("");
    try {
      const r = await apiPost<any>("/api/telegram/channels", { action, channel, enabled });
      setTgChannels(r.items || []);
      if (action === "add") setTgInput("");
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(""); }
  }

  async function tgDiscover() {
    setTgBusy("discover"); setError("");
    try {
      const r = await apiPost<any>("/api/telegram/discover", {});
      setTgCandidates(r.items || []);
      setSaved(`Найдено каналов: ${r.total || 0}`);
    } catch (e) { setError((e as Error).message); }
    finally { setTgBusy(""); }
  }
  async function tgValidate() {
    setTgBusy("validate"); setError("");
    try {
      const r = await apiPost<any>("/api/telegram/validate", { limit: 20 });
      setTgCandidates(r.items || []);
      setSaved(`Проверено: ${r.checked || 0}, квалифицировано: ${r.qualified || 0}`);
    } catch (e) { setError((e as Error).message); }
    finally { setTgBusy(""); }
  }
  async function tgValidateAll() {
    setTgBusy("validate"); setError("");
    try {
      let last: any = null;
      for (let i = 0; i < 5; i += 1) {
        last = await apiPost<any>("/api/telegram/validate", { limit: 50 });
        setTgCandidates(last.items || []);
        const pending = (last.items || []).filter((x: any) => ["discovered", "validating"].includes(x.status)).length;
        if (!pending) break;
      }
      setSaved(`Проверка завершена. Квалифицировано: ${(last?.items || []).filter((x: any) => x.status === "qualified").length}`);
    } catch (e) { setError((e as Error).message); }
    finally { setTgBusy(""); }
  }
  async function tgReprocess() {
    setTgBusy("reprocess"); setError("");
    try {
      const r = await apiPost<any>("/api/telegram/reprocess", { limit: 500 });
      setSaved(`Переобработано: ${r.checked}, изменено: ${r.changed}`);
    } catch (e) { setError((e as Error).message); }
    finally { setTgBusy(""); }
  }
  async function tgEnableRecommended() {
    setTgBusy("recommend"); setError("");
    try {
      const r = await apiPost<any>("/api/telegram/enable-recommended", { limit: 110, min_quality: 45 });
      setTgChannels(r.channels || []);
      const c = await apiGet<any>("/api/telegram/candidates");
      setTgCandidates(c.items || []);
      setSaved(`Подключено рекомендованных: ${r.count || 0}`);
    } catch (e) { setError((e as Error).message); }
    finally { setTgBusy(""); }
  }
  async function tgDisableIrrelevant() {
    setTgBusy("irrelevant"); setError("");
    try {
      const r = await apiPost<any>("/api/telegram/disable-irrelevant", {});
      setTgChannels(r.channels || []);
      setSaved(`Отключено нерелевантных: ${r.count || 0}`);
    } catch (e) { setError((e as Error).message); }
    finally { setTgBusy(""); }
  }
  async function tgAddBulk() {
    if (!tgBulk.trim()) return;
    setTgBusy("bulk"); setError("");
    try {
      const r = await apiPost<any>("/api/telegram/add-bulk", { text: tgBulk });
      setTgCandidates(r.items || []);
      setTgBulk("");
      setSaved(`Добавлено кандидатов: ${r.added || 0}`);
    } catch (e) { setError((e as Error).message); }
    finally { setTgBusy(""); }
  }

  async function hhRefresh() {
    setTgBusy("hh"); setError("");
    try { setHh(await apiGet<any>("/api/hh/status")); }
    catch (e) { setError((e as Error).message); }
    finally { setTgBusy(""); }
  }

  async function tgEnableSelected() {
    if (!tgSelected.length) return;
    setTgBusy("enable"); setError("");
    try {
      const r = await apiPost<any>("/api/telegram/enable", { channels: tgSelected });
      setTgChannels(r.channels || []);
      setTgSelected([]);
      const c = await apiGet<any>("/api/telegram/candidates");
      setTgCandidates(c.items || []);
      setSaved(`Подключено каналов: ${r.count || 0}`);
    } catch (e) { setError((e as Error).message); }
    finally { setTgBusy(""); }
  }
  async function tgPause() {
    setTgBusy("pause"); setError("");
    try {
      const r = await apiPost<any>("/api/telegram/pause-inactive", { days: 45 });
      setTgChannels(r.channels || []);
      setSaved(`Приостановлено: ${r.count || 0}`);
    } catch (e) { setError((e as Error).message); }
    finally { setTgBusy(""); }
  }
  async function tgImport(file: File) {
    const text = await file.text();
    setTgBusy("import"); setError("");
    try {
      const r = await apiPost<any>("/api/telegram/discover", { csv: text });
      setTgCandidates(r.items || []);
      setSaved(`Импортировано, всего кандидатов: ${r.total || 0}`);
    } catch (e) { setError((e as Error).message); }
    finally { setTgBusy(""); }
  }

  function saveAutomation() {
    if (!data || !autoCfg) return;
    const { startups_hours, ...automation } = autoCfg;
    const settings = {
      ...data.settings, automation,
      startups: { ...(data.settings.startups || {}), hours: Number(startups_hours || 24) },
    };
    run("automation", () => apiPost("/api/settings", { settings }), "Расписание сохранено");
  }

  function saveSettings() {
    run("settings", async () => {
      const parsed = JSON.parse(settingsJson);
      await apiPost("/api/settings", { settings: parsed });
    }, "Настройки сохранены");
  }

  function saveQwenProvider(provider: string) {
    if (!data) return;
    const settings = { ...data.settings, qwen: { ...(data.settings.qwen || {}), provider } };
    run("qwen-provider", () => apiPost("/api/settings", { settings }), "Провайдер генерации сохранён");
  }

  function saveCloudPrivacy(allow: boolean) {
    if (!data) return;
    const settings = {
      ...data.settings,
      privacy: { ...(data.settings.privacy || {}), allow_cloud_llm: allow },
    };
    run("qwen-privacy", () => apiPost("/api/settings", { settings }),
        allow ? "Облачная обработка разрешена" : "Облачная обработка запрещена");
  }

  function saveQwenLimits() {
    if (!data) return;
    const settings = {
      ...data.settings,
      llm: {
        ...(data.settings.llm || {}),
        aitunnel: {
          ...(data.settings.llm?.aitunnel || {}),
          ...(qwenForm.monthly !== "" ? { monthly_budget_rub: Number(qwenForm.monthly) } : {}),
        },
      },
      qwen: {
        ...(data.settings.qwen || {}),
        cloud: {
          ...(data.settings.qwen?.cloud || {}),
          max_letter_rub: Number(qwenForm.letter || 1.5),
          max_daily_rub: Number(qwenForm.daily || 30),
          allow_fallback: !!qwenForm.fallback,
          models: { cheap: qwenForm.cheap, quality: qwenForm.quality },
        },
      },
    };
    run("qwen-limits", () => apiPost("/api/settings", { settings }), "Лимиты и модели сохранены");
  }

  function saveSubmissionChannel(channel: string, patch: Record<string, unknown>) {
    if (!data) return;
    if (patch.auto === true && !window.confirm(
      "Включить автоматическую отправку через этот канал? Заявки будут уходить без вашего подтверждения в рамках политики автопилота.")) return;
    const channels = { ...(data.settings.submission?.channels || {}) };
    channels[channel] = { ...(channels[channel] || {}), ...patch };
    const settings = { ...data.settings, submission: { ...(data.settings.submission || {}), channels } };
    run(`channel-${channel}`, () => apiPost("/api/settings", { settings }), "Каналы отправки сохранены");
  }

  function saveSubmissionDomains() {
    if (!data) return;
    const domains = submissionDomains.split(",").map((x) => x.trim().toLowerCase()).filter(Boolean);
    const channels = { ...(data.settings.submission?.channels || {}) };
    for (const channel of ["ats_form", "career_site_form"]) {
      channels[channel] = { ...(channels[channel] || {}), domains };
    }
    const settings = { ...data.settings, submission: { ...(data.settings.submission || {}), channels } };
    run("submission-domains", () => apiPost("/api/settings", { settings }), "Домены сохранены");
  }

  async function saveEmailConfig(check = false) {
    setBusy("email-config"); setSaved(""); setError("");
    try {
      const result = await apiPost<any>("/api/submission/email", emailForm);
      if (!result.ok) throw new Error(result.reason || "Не удалось сохранить почту");
      emailDirtyRef.current = false;
      setEmailForm((current: any) => ({ ...current, password: "" }));
      if (check) {
        const tested = await apiPost<any>("/api/submission/email/test", {});
        if (!tested.ok) throw new Error(`Сохранено, но проверка не прошла: ${tested.reason || "SMTP недоступен"}`);
        setSaved("Почта сохранена, SMTP ответил. Пароль хранится в Windows DPAPI.");
      } else {
        setSaved("Почта сохранена. Пароль хранится в Windows DPAPI.");
      }
      await load();
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  function updateEmail(key: string, value: unknown) {
    emailDirtyRef.current = true;
    setEmailForm((current: any) => ({ ...current, [key]: value }));
  }

  async function saveQwenKey() {
    if (!qwenKey.trim()) return;
    setBusy("qwen-key"); setSaved(""); setError("");
    try {
      await apiPost("/api/qwen/key", { key: qwenKey.trim() });
      setQwenKey("");
      setShowQwenKey(false);
      setSaved("Ключ AITUNNEL сохранён в защищённом хранилище Windows");
      await load();
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  async function testQwen() {
    setBusy("qwen-test"); setError(""); setSaved("");
    try {
      const result = await apiPost<any>("/api/qwen/test", {});
      setQwenTest(result);
      setSaved(result.ok ? "Тестовое письмо готово" : "Нужна проверка: письмо не прошло валидацию");
      await load();
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }

  function saveNotifications(enabled: boolean) {
    if (!data) return;
    const settings = {
      ...data.settings,
      notifications: {
        ...(data.settings.notifications || {}),
        telegram: { ...(data.settings.notifications?.telegram || {}), enabled },
      },
    };
    run("notifications", () => apiPost("/api/settings", { settings }), enabled ? "Уведомления включены" : "Уведомления выключены");
  }

  if (error && !data) return <ErrorState error={error} onRetry={load} />;
  if (!data) return <LoadingState rows={4} />;
  const s = data.settings;
  const ai = data.llm;
  const qw = data.qwen || {};
  const qwLocal = qw.local || {};
  const qwCloud = qw.cloud || {};
  const qwSpend = qw.spend || {};
  const sub = data.submission || {};
  const cm = data.candidate_memory || {};
  const integrations = cm.integrations || {};
  const spend = ai.month_spend_rub ?? ai.month_spend_usd ?? 0;
  const currency = ai.month_spend_rub !== undefined ? "₽" : "$";

  return (
    <div className="animate-rise">
      <PageHeader eyebrow="Настройки" title="Настройки"
        description="Профиль, предпочтения и подключения. Технические детали — в разделе «Дополнительно»." />
      {saved && <div className="mb-3 rounded-lg border border-emerald-500/25 bg-emerald-500/10 p-3 text-[12px] text-emerald-700 dark:text-emerald-300">{saved}</div>}
      {error && <div className="mb-3 rounded-lg border border-destructive/25 bg-destructive/5 p-3 text-[12px] text-destructive">{error}</div>}

      <div className="mb-4 grid gap-2 sm:grid-cols-3">
        <a href="/resume" className="rounded-xl border bg-card p-4 transition-colors hover:border-primary/30"><b className="text-[13px]">Резюме</b><div className="text-[11.5px] text-muted-foreground">Факты, проекты и версии резюме</div></a>
        <a href="/autopilot" className="rounded-xl border bg-card p-4 transition-colors hover:border-primary/30"><b className="text-[13px]">Подключения</b><div className="text-[11.5px] text-muted-foreground">hh.ru, браузерная сессия и автопилот</div></a>
        <a href="/statistics" className="rounded-xl border bg-card p-4 transition-colors hover:border-primary/30"><b className="text-[13px]">Аналитика</b><div className="text-[11.5px] text-muted-foreground">Результаты откликов и импорт истории</div></a>
      </div>

      <Tabs value={tab} onValueChange={(value) => setSearchParams(value === "profile" ? {} : { tab: value })}>
        <TabsList><TabsTrigger value="profile">Мой профиль</TabsTrigger><TabsTrigger value="preferences">Предпочтения</TabsTrigger><TabsTrigger value="search">Подключения</TabsTrigger><TabsTrigger value="ai">AI и бюджет</TabsTrigger><TabsTrigger value="notifications">Уведомления</TabsTrigger><TabsTrigger value="diagnostics">Дополнительно</TabsTrigger></TabsList>

        <TabsContent value="profile">
          <Card><CardHeader><CardTitle>Мой профиль</CardTitle><div className="text-[12px] text-muted-foreground">Что о вас знает поиск. Используется в оценке вакансий и письмах.</div></CardHeader><CardContent className="space-y-3">
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <label className="text-[12px]">Подтверждённые навыки<Input value={profileForm?.skills || ""} onChange={(e) => setProfileForm({ ...profileForm, skills: e.target.value })} /></label>
              <label className="text-[12px]">Целевые роли<Input value={profileForm?.roles || ""} onChange={(e) => setProfileForm({ ...profileForm, roles: e.target.value })} /></label>
            </div>
            <label className="block text-[12px]">Доступность<Input placeholder="например, 20 часов в неделю" value={profileForm?.availability || ""} onChange={(e) => setProfileForm({ ...profileForm, availability: e.target.value })} /></label>
            <div className="grid grid-cols-1 gap-3 border-t pt-3 md:grid-cols-2">
              <label className="text-[12px]">Имя для заявок<Input value={contactForm.first_name || ""} onChange={(e) => setContactForm({ ...contactForm, first_name: e.target.value })} /></label>
              <label className="text-[12px]">Фамилия<Input value={contactForm.last_name || ""} onChange={(e) => setContactForm({ ...contactForm, last_name: e.target.value })} /></label>
              <label className="text-[12px]">Email для работодателя<Input type="email" value={contactForm.email || ""} onChange={(e) => setContactForm({ ...contactForm, email: e.target.value })} /></label>
              <label className="text-[12px]">Телефон<Input value={contactForm.phone || ""} onChange={(e) => setContactForm({ ...contactForm, phone: e.target.value })} /></label>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button onClick={saveProfile} disabled={busy === "profile"}><Save className="h-4 w-4" />Сохранить профиль</Button>
              <Button variant="outline" onClick={saveContact} disabled={busy === "contact"}><Save className="h-4 w-4" />Сохранить контакты</Button>
            </div>
          </CardContent></Card>
          <Card className="mt-4">
            <CardHeader><CardTitle>Мои данные и память</CardTitle><div className="text-[12px] text-muted-foreground">Один раз подтверждённые данные используются в письмах, формах и после перезапуска.</div></CardHeader>
            <CardContent className="space-y-3">
              <div className="grid grid-cols-2 gap-2 text-[12px] sm:grid-cols-3">
                <div className="rounded-lg border p-2.5"><div className="text-muted-foreground">Контакты</div><b>{cm.contact_filled ? "заполнены" : "не хватает: " + (cm.missing_contact || []).join(", ")}</b></div>
                <div className="rounded-lg border p-2.5"><div className="text-muted-foreground">Резюме</div><b>{cm.skills || 0} навыков · {cm.projects || 0} проектов</b></div>
                <div className="rounded-lg border p-2.5"><div className="text-muted-foreground">Предпочтения</div><b>{cm.preferences ? "настроены" : "не настроены"}</b></div>
                <div className="rounded-lg border p-2.5"><div className="text-muted-foreground">Qwen</div><b>{integrations.qwen?.cloud ? "локальная + AITUNNEL" : integrations.qwen?.local ? "локальная" : "недоступна"}</b></div>
                <div className="rounded-lg border p-2.5"><div className="text-muted-foreground">AITUNNEL</div><b>{integrations.aitunnel_key ? "ключ сохранён" : "ключ отсутствует"}</b></div>
                <div className="rounded-lg border p-2.5"><div className="text-muted-foreground">SMTP</div><b>{integrations.smtp ? "подключён" : "не подключён"}</b></div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" onClick={syncMemory} disabled={busy === "memory-sync"}>Обновить память</Button>
                <Button size="sm" variant="ghost" onClick={() => setMemoryOpen((v) => !v)}>{memoryOpen ? "Скрыть факты" : `Подтверждённые факты (${memoryFacts.length})`}</Button>
              </div>
              {memoryFacts.some((f) => f.key.startsWith("skill.")) && (
                <div className="flex flex-wrap gap-1.5">
                  {memoryFacts.filter((f) => f.key.startsWith("skill.")).slice(0, 24).map((fact) => {
                    const raw = String(fact.value || "");
                    const level = raw.includes(":") ? raw.split(":").pop()?.trim() || "" : "";
                    return <Badge key={fact.key} variant="primary" className="font-normal">
                      {fact.key.slice(6)}{level ? ` · ${level}` : ""}
                    </Badge>;
                  })}
                </div>
              )}
              <details className="rounded-lg border p-3">
                <summary className="cursor-pointer text-[12px] font-medium text-muted-foreground hover:text-foreground">Импорт резюме и навыки</summary>
                <div className="mt-3 space-y-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <label className="inline-flex h-9 cursor-pointer items-center rounded-lg border bg-card px-3 text-[12px] font-medium hover:bg-accent">
                      {busy === "resume-import" ? "Разбираю…" : "Импортировать резюме"}
                      <input type="file" accept=".pdf,.docx,.txt,.md,.json" className="sr-only" disabled={busy === "resume-import"}
                             onChange={(e) => { const f = e.target.files?.[0]; if (f) importResume(f); e.target.value = ""; }} />
                    </label>
                    <span className="text-[11px] text-muted-foreground">PDF, DOCX, TXT, JSON · локальная Qwen</span>
                  </div>
                  {importResult && (
                    <div className="rounded-lg border bg-muted/20 p-2.5 text-[11.5px]">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span>{importResult.duplicate ? "Этот файл уже импортирован" : `Найдено изменений: ${importResult.items?.length || 0}`}</span>
                        {!importResult.duplicate && importResult.items?.length ? <Button size="sm" onClick={confirmImport} disabled={!importSelected.length || busy === "resume-confirm"}>Подтвердить выбранное</Button> : null}
                      </div>
                      {importResult.items?.map((item: any) => (
                        <label key={item.key} className="mt-1 flex items-start gap-2 rounded px-1 py-1 hover:bg-accent/40">
                          <input type="checkbox" checked={importSelected.includes(item.key)}
                                 onChange={(e) => setImportSelected((current) => e.target.checked ? [...current, item.key] : current.filter((x) => x !== item.key))} />
                          <span className="min-w-0 flex-1"><b>{item.label}</b> <span className="text-muted-foreground">{item.status === "new" ? "новое" : item.status === "conflict" ? "конфликт уровня" : "изменение"}</span>
                            <span className="block truncate text-muted-foreground">{String(item.new || "").slice(0, 140)}</span></span>
                        </label>
                      ))}
                    </div>
                  )}
                  <div className="border-t pt-3">
                    <div className="flex flex-wrap gap-2">
                      <Input className="min-w-[220px] flex-1" placeholder="Например: Начал изучать PyTorch" value={skillText} onChange={(e) => setSkillText(e.target.value)} />
                      <Button size="sm" variant="outline" onClick={proposeSkill} disabled={!skillText.trim() || busy === "skill-propose"}>Предложить</Button>
                    </div>
                    {skillProposal && (
                      <div className="mt-2 rounded-lg border p-2.5 text-[11.5px]">
                        {skillProposal.ok ? (
                          <div className="flex flex-wrap items-center gap-2">
                            <b>{skillProposal.skill}</b>
                            <select className="h-8 rounded-md border bg-card px-2 text-[11.5px]" value={skillProposal.status} onChange={(e) => setSkillProposal({ ...skillProposal, status: e.target.value })}>
                              <option value="learning">Изучаю</option><option value="basic">Базовые знания</option><option value="practical">Практический опыт</option><option value="confident">Уверенно владею</option><option value="unknown">Не указан</option>
                            </select>
                            <Button size="sm" onClick={applySkill} disabled={busy === "skill-apply"}>Подтвердить</Button>
                          </div>
                        ) : <span className="text-muted-foreground">{skillProposal.reason}</span>}
                      </div>
                    )}
                  </div>
                  <div className="border-t pt-3">
                    <div className="mb-1 text-[11.5px] font-medium text-muted-foreground">Добавить проект</div>
                    <div className="grid gap-2 sm:grid-cols-2">
                      <Input placeholder="Название проекта" value={projectForm.name} onChange={(e) => setProjectForm({ ...projectForm, name: e.target.value })} />
                      <Input placeholder="Стек через запятую" value={projectForm.stack} onChange={(e) => setProjectForm({ ...projectForm, stack: e.target.value })} />
                    </div>
                    <div className="mt-2 flex gap-2">
                      <Input className="flex-1" placeholder="Короткое подтверждённое описание" value={projectForm.description} onChange={(e) => setProjectForm({ ...projectForm, description: e.target.value })} />
                      <Button size="sm" variant="outline" onClick={addProject} disabled={!projectForm.name.trim() || busy === "project-add"}>Добавить</Button>
                    </div>
                  </div>
                  <div className="border-t pt-3">
                    <Button size="sm" variant="outline" onClick={previewResumeUpdate} disabled={busy === "resume-preview"}>Обновить резюме из памяти</Button>
                    {resumePreview && (
                      <div className="mt-2 rounded-lg border p-2.5 text-[11.5px]">
                        <div>Новых навыков: {resumePreview.skills?.length || 0} · проектов: {resumePreview.projects?.length || 0}</div>
                        <div className="mt-1 text-muted-foreground">{resumePreview.skills?.map((x: any) => x.name).join(", ")}</div>
                        <Button size="sm" className="mt-2" onClick={applyResumeUpdate} disabled={busy === "resume-update"}>Добавить в резюме</Button>
                      </div>
                    )}
                  </div>
                  <div className="border-t pt-3">
                    <div className="mb-1 text-[11.5px] font-medium text-muted-foreground">История обучения</div>
                    <div className="flex flex-wrap gap-2">
                      <Input className="min-w-[220px] flex-1" placeholder="Курс, проект или освоенная технология" value={newLearningText} onChange={(e) => setNewLearningText(e.target.value)} />
                      <Button size="sm" variant="outline" onClick={addLearningEntry} disabled={!newLearningText.trim() || busy === "learning-add"}>Добавить</Button>
                    </div>
                    {learning.length ? <div className="mt-2 max-h-[160px] space-y-1 overflow-auto">{learning.slice(0, 8).map((item) => (
                      <div key={item.id} className="rounded px-2 py-1 text-[11px] hover:bg-accent/40"><b>{String(item.created_at).slice(0, 10)}</b> · {item.description}</div>
                    ))}</div> : <div className="mt-2 text-[11px] text-muted-foreground">Записей пока нет.</div>}
                  </div>
                </div>
              </details>
              {memoryOpen && (
                <div className="max-h-[300px] space-y-1 overflow-auto rounded-lg border p-2">
                  {memoryFacts.length ? memoryFacts.map((fact) => (
                    <div key={fact.key} className="flex items-start gap-2 rounded px-2 py-1.5 text-[11.5px] hover:bg-accent/40">
                      <div className="min-w-0 flex-1"><b>{fact.key}</b><div className="truncate text-muted-foreground">{String(fact.value || "").slice(0, 160)}</div><div className="text-[10px] text-muted-foreground">{fact.source} · {String(fact.updated_at || "").slice(0, 16)}</div></div>
                      {fact.key.startsWith("skill.") && <Button size="sm" variant="ghost" onClick={() => { setSkillText(fact.key.slice(6)); setSkillProposal({ ok: true, skill: fact.key.slice(6), status: "unknown", evidence: "Изменение подтверждённого навыка" }); }}>Изменить</Button>}
                      <Button size="sm" variant="ghost" onClick={() => forgetFact(fact.key)}>Удалить</Button>
                    </div>
                  )) : <div className="px-2 py-4 text-center text-[12px] text-muted-foreground">Подтверждённых фактов пока нет.</div>}
                </div>
              )}
              <div className="grid gap-2 sm:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)_auto]">
                <Input value={newFactKey} onChange={(e) => setNewFactKey(e.target.value)} placeholder="decision.office" />
                <Input value={newFactValue} onChange={(e) => setNewFactValue(e.target.value)} placeholder="Подтверждённое правило или факт" />
                <Button size="sm" variant="outline" onClick={addMemoryFact} disabled={!newFactKey.trim() || !newFactValue.trim() || busy === "memory-add"}>Добавить факт</Button>
              </div>
              <details className="rounded-lg border p-2.5">
                <summary className="cursor-pointer text-[11.5px] text-muted-foreground hover:text-foreground">Управление подключениями</summary>
                <div className="mt-2 flex flex-wrap gap-2">
                  <Button size="sm" variant="ghost" onClick={() => forgetSecret("aitunnel")} disabled={busy === "secret-aitunnel"}>Отключить AITUNNEL</Button>
                  <Button size="sm" variant="ghost" onClick={() => forgetSecret("smtp")} disabled={busy === "secret-smtp"}>Удалить SMTP-пароль</Button>
                </div>
              </details>
              <p className="text-[11px] text-muted-foreground">Пароли и API-ключи не входят в память и не показываются. Контактные данные хранятся отдельно от RAG.</p>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="preferences">
          <Card><CardHeader><CardTitle>Предпочтения работы</CardTitle><div className="text-[12px] text-muted-foreground">Города, формат и условия. Влияют на подбор и приоритет вакансий.</div></CardHeader><CardContent className="space-y-3">
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <label className="text-[12px]">Города<Input value={profileForm?.locations || ""} onChange={(e) => setProfileForm({ ...profileForm, locations: e.target.value })} /></label>
              <label className="text-[12px]">Форматы<Input value={profileForm?.modes || ""} onChange={(e) => setProfileForm({ ...profileForm, modes: e.target.value })} /></label>
              <label className="text-[12px]">Зарплата, ₽<Input type="number" value={profileForm?.salary || ""} onChange={(e) => setProfileForm({ ...profileForm, salary: e.target.value })} /></label>
              <label className="text-[12px]">Исключённые должности<Input value={profileForm?.excluded || ""} onChange={(e) => setProfileForm({ ...profileForm, excluded: e.target.value })} /></label>
              <label className="text-[12px]">Занятость (через запятую)<Input placeholder="стажировка, частичная, полная" value={profileForm?.employment || ""} onChange={(e) => setProfileForm({ ...profileForm, employment: e.target.value })} /></label>
              <label className="text-[12px]">Желаемый график<Input placeholder="гибкий, 5/2, сменный" value={profileForm?.schedule || ""} onChange={(e) => setProfileForm({ ...profileForm, schedule: e.target.value })} /></label>
            </div>
            <div className="flex flex-wrap gap-4 text-[12px]">
              <label className="flex items-center gap-2"><input type="checkbox" checked={!!profileForm?.business_trips} onChange={(e) => setProfileForm({ ...profileForm, business_trips: e.target.checked })} />Готов к командировкам</label>
              <label className="flex items-center gap-2"><input type="checkbox" checked={!!profileForm?.relocation} onChange={(e) => setProfileForm({ ...profileForm, relocation: e.target.checked })} />Готов к переезду</label>
            </div>
            <Button onClick={saveProfile} disabled={busy === "profile"}><Save className="h-4 w-4" />Сохранить предпочтения</Button>
          </CardContent></Card>
        </TabsContent>

        <TabsContent value="search">
          <Card className="mb-4">
            <CardHeader><CardTitle>Каналы отправки</CardTitle><div className="text-[12px] text-muted-foreground">Автопилот отправляет только по включённым каналам. CAPTCHA, вход и закрытые вакансии всегда остаются ручным шагом.</div></CardHeader>
            <CardContent className="space-y-2">
              {Object.entries(sub || {}).map(([key, channel]: [string, any]) => (
                <div key={key} className="flex flex-wrap items-center gap-3 rounded-lg border p-3 text-[12px]">
                  <div className="min-w-0 flex-1">
                    <b>{channel.label || key}</b>
                    <div className="text-[11px] text-muted-foreground">{channel.reason || (channel.auto_allowed ? "готова к автоматической отправке" : channel.available ? "доступна для подготовки" : "не подключена")}</div>
                  </div>
                  <label className="flex items-center gap-1.5 text-muted-foreground">
                    <input type="checkbox" checked={!!channel.enabled} onChange={(e) => saveSubmissionChannel(key, { enabled: e.target.checked })} disabled={busy === `channel-${key}` || key === "manual_assisted"} />
                    включён
                  </label>
                  {key !== "manual_assisted" && (
                    <label className="flex items-center gap-1.5 text-muted-foreground">
                      <input type="checkbox" checked={!!channel.auto} onChange={(e) => saveSubmissionChannel(key, { auto: e.target.checked })} disabled={busy === `channel-${key}`} />
                      авто
                    </label>
                  )}
                </div>
              ))}
              <div className="flex flex-wrap items-center gap-2 pt-1">
                <Input className="min-w-[240px] flex-1" placeholder="домены форм: career.habr.com, job.mts.ru" value={submissionDomains} onChange={(e) => setSubmissionDomains(e.target.value)} />
                <Button size="sm" variant="outline" onClick={saveSubmissionDomains} disabled={busy === "submission-domains"}>Сохранить домены</Button>
              </div>
              <p className="text-[11px] text-muted-foreground">Формы без CAPTCHA и с подтверждённой страницей успеха могут отправляться автоматически. Habr и VK используют CAPTCHA и остаются ручными.</p>
              <details open={emailOpen} onToggle={(e) => setEmailOpen((e.currentTarget as HTMLDetailsElement).open)} className="rounded-lg border p-3">
                <summary className="cursor-pointer text-[12px] font-medium text-muted-foreground hover:text-foreground">Почта для откликов</summary>
                <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  <label className="text-[11.5px] text-muted-foreground">SMTP-сервер
                    <Input className="mt-1" placeholder="smtp.yandex.ru" value={emailForm.smtp_host} onChange={(e) => updateEmail("smtp_host", e.target.value)} />
                  </label>
                  <label className="text-[11.5px] text-muted-foreground">Порт
                    <Input type="number" className="mt-1" value={emailForm.smtp_port} onChange={(e) => updateEmail("smtp_port", Number(e.target.value))} />
                  </label>
                  <label className="text-[11.5px] text-muted-foreground">Защита
                    <Select className="mt-1" value={emailForm.smtp_security} onChange={(e) => updateEmail("smtp_security", e.target.value)}>
                      <option value="starttls">STARTTLS (587)</option>
                      <option value="ssl">SSL (465)</option>
                      <option value="none">Без шифрования</option>
                    </Select>
                  </label>
                  <label className="text-[11.5px] text-muted-foreground">Логин SMTP
                    <Input className="mt-1" value={emailForm.smtp_username} onChange={(e) => updateEmail("smtp_username", e.target.value)} autoComplete="username" />
                  </label>
                  <label className="text-[11.5px] text-muted-foreground">Email отправителя
                    <Input type="email" className="mt-1" value={emailForm.from_email} onChange={(e) => updateEmail("from_email", e.target.value)} />
                  </label>
                  <label className="text-[11.5px] text-muted-foreground">Имя отправителя
                    <Input className="mt-1" value={emailForm.from_name} onChange={(e) => updateEmail("from_name", e.target.value)} />
                  </label>
                  <label className="text-[11.5px] text-muted-foreground">Пароль приложения
                    <Input type="password" className="mt-1" placeholder={sub.email?.available ? "сохранён" : "пароль приложения"} value={emailForm.password} onChange={(e) => updateEmail("password", e.target.value)} autoComplete="new-password" />
                  </label>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <Button size="sm" variant="outline" onClick={() => saveEmailConfig(false)} disabled={busy === "email-config"}>Сохранить почту</Button>
                  <Button size="sm" onClick={() => saveEmailConfig(true)} disabled={busy === "email-config"}>Сохранить и проверить</Button>
                  <span className="text-[11px] text-muted-foreground">{sub.email?.available ? "Подключена: " + (sub.email?.reason || "готова") : sub.email?.reason || "не подключена"} · приём SMTP не равен доставке в ящик</span>
                </div>
              </details>
            </CardContent>
          </Card>
          <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
            <Card><CardHeader><CardTitle>Порог и очередь</CardTitle></CardHeader><CardContent className="space-y-2 text-[12.5px]">
              <div className="flex justify-between"><span className="text-muted-foreground">Порог просмотра</span><b>{s.threshold}</b></div>
              <div className="flex justify-between"><span className="text-muted-foreground">Порог очереди</span><b>{s.queue?.min_score}</b></div>
              <div className="flex justify-between"><span className="text-muted-foreground">Лимит в день</span><b>{s.queue?.daily_limit}</b></div>
              <div className="flex justify-between"><span className="text-muted-foreground">Минут в день</span><b>{s.queue?.daily_minutes}</b></div>
              <p className="pt-1 text-[11.5px] text-muted-foreground">Значения меняются в профиле и предпочтениях. Полный JSON — в разделе «Дополнительно».</p>
            </CardContent></Card>
            <Card><CardHeader><CardTitle>Источники</CardTitle></CardHeader><CardContent className="space-y-2">
              {Object.entries(data.source_status || {}).map(([name, st]) => {
                const statusMap: Record<string, { label: string; variant: "success" | "warning" | "danger" | "default" }> = {
                  ok: { label: "работает", variant: "success" },
                  partial: { label: "частично", variant: "warning" },
                  blocked: { label: "заблокирован", variant: "danger" },
                  error: { label: "ошибка", variant: "danger" },
                };
                const status = !st.available
                  ? { label: "недоступен", variant: "warning" as const }
                  : (!st.configured
                     ? { label: "выключен", variant: "default" as const }
                     : (statusMap[st.last_status] || { label: "не запускался", variant: "default" as const }));
                return (
                  <div key={name} className="rounded-md border p-3 text-[12px]">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex min-w-0 items-center gap-2">
                        <Badge variant={status.variant}>{status.label}</Badge>
                        <b className="truncate">{st.label}</b>
                      </div>
                      {st.configured && st.available ? (
                        <Button size="sm" variant="outline"
                          onClick={() => run("run-" + name, () => apiPost("/api/sources/run", { source: name }), `Источник «${st.label}» запущен`)}
                          disabled={busy === "run-" + name}>Запустить</Button>
                      ) : <span className="text-[11px] text-muted-foreground">Не подключён</span>}
                    </div>
                    <div className="mt-1 text-[11px] text-muted-foreground">{st.description}</div>
                    <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
                      <span title="Сумма полученных карточек за все запуски, включая повторы; не число уникальных активных вакансий">получено суммарно (с повторами): {st.items}</span>
                      <span>последний запуск: {st.last_run_at ? dt(st.last_run_at) : "—"}</span>
                      {!st.available && st.reason && <span className="text-amber-600 dark:text-amber-400">{st.reason}</span>}
                      {st.last_error && <span className="text-destructive">{st.last_error}</span>}
                    </div>
                  </div>
                );
              })}
            </CardContent></Card>
            <Card className="lg:col-span-2"><CardHeader><CardTitle>Telegram-каналы</CardTitle></CardHeader><CardContent className="space-y-2">
              <div className="flex flex-wrap gap-2">
                <Input className="min-w-[220px] flex-1" placeholder="https://t.me/channel или @channel" value={tgInput} onChange={(e) => setTgInput(e.target.value)} />
                <Button size="sm" variant="outline" onClick={() => tgAction("add", tgInput)} disabled={!tgInput.trim() || !!busy || !!tgBusy}>Добавить</Button>
                <Button size="sm" variant="outline" onClick={() => run("run-telegram", () => apiPost("/api/sources/run", { source: "telegram" }), "Telegram обновлён")} disabled={!!busy || !!tgBusy}>Обновить</Button>
              </div>
              <div className="flex flex-wrap items-center gap-2 border-y py-2">
                <Button size="sm" variant="outline" onClick={tgDiscover} disabled={!!tgBusy}>{tgBusy === "discover" ? "Ищу…" : "Найти новые каналы"}</Button>
                <Button size="sm" variant="outline" onClick={tgValidateAll} disabled={!!tgBusy}>{tgBusy === "validate" ? "Проверяю…" : "Проверить все ожидающие"}</Button>
                <Button size="sm" variant="outline" onClick={tgEnableRecommended} disabled={!!tgBusy}>{tgBusy === "recommend" ? "Подключаю…" : "Подключить рекомендованные"}</Button>
                <Button size="sm" variant="ghost" onClick={tgDisableIrrelevant} disabled={!!tgBusy}>Выключить нерелевантные</Button>
                <Button size="sm" variant="ghost" onClick={tgReprocess} disabled={!!tgBusy}>{tgBusy === "reprocess" ? "Обрабатываю…" : "Переобработать тексты"}</Button>
                <Input type="file" accept=".csv,.txt" className="h-8 w-[180px] p-1 text-[11px]" onChange={(e) => { const f = e.target.files?.[0]; if (f) tgImport(f); }} />
                <Button size="sm" variant="ghost" onClick={tgPause} disabled={!!tgBusy}>Отключить неактивные</Button>
              </div>
              <div className="flex gap-2">
                <Textarea className="min-h-[54px] text-[11.5px]" placeholder="Массовое добавление: по одному @channel / t.me/channel на строку" value={tgBulk} onChange={(e) => setTgBulk(e.target.value)} />
                <Button size="sm" variant="outline" onClick={tgAddBulk} disabled={!tgBulk.trim() || !!tgBusy}>Добавить список</Button>
              </div>
              <div className="text-[11.5px] text-muted-foreground">
                Кандидатов: {tgCandidates.length}. Обнаруженные каналы не включаются автоматически.
              </div>
              {tgSummary && (
                <div className="grid grid-cols-2 gap-2 text-[11.5px] sm:grid-cols-3 lg:grid-cols-5">
                  {[
                    ["Обнаружено", tgSummary.candidates?.total],
                    ["Проверено", (tgSummary.candidates?.total || 0) - (tgSummary.candidates?.discovered || 0)],
                    ["Качественных", tgSummary.candidates?.qualified],
                    ["Активных", tgSummary.channels?.active],
                    ["Недоступно", tgSummary.candidates?.unavailable],
                    ["Вакансий", tgSummary.jobs?.unique_jobs],
                    ["Подходящих", tgSummary.jobs?.eligible],
                    ["Обогащено", tgSummary.jobs?.enriched],
                    ["Обновлено", tgSummary.last_run_at ? dt(tgSummary.last_run_at) : "—"],
                  ].map(([label, value]) => (
                    <div key={String(label)} className="rounded-md border px-2 py-1.5">
                      <div className="text-muted-foreground">{label}</div><b>{value ?? 0}</b>
                    </div>
                  ))}
                </div>
              )}
              {tgCandidates.length > 0 && (
                <div className="max-h-[300px] space-y-1 overflow-auto rounded-md border p-2">
                  {tgCandidates.slice(0, 60).map((c) => (
                    <label key={c.channel} className="flex items-center gap-2 rounded px-1 py-0.5 text-[11.5px] hover:bg-accent">
                      <input type="checkbox" disabled={c.status !== "qualified"}
                        checked={tgSelected.includes(c.channel)}
                        onChange={() => setTgSelected((s) => s.includes(c.channel) ? s.filter((x) => x !== c.channel) : [...s, c.channel])} />
                      <Badge variant={c.status === "qualified" ? "success" : c.status === "enabled" ? "primary" : c.status === "unavailable" || c.status === "irrelevant" ? "danger" : "default"}>{c.status}</Badge>
                      <span className="min-w-0 flex-1 truncate">@{c.channel} <span className="text-muted-foreground">{c.topic}</span></span>
                      <span className="text-muted-foreground">вак: {c.vacancies} · стаж: {c.internships} · q: {Math.round(c.quality || 0)}</span>
                    </label>
                  ))}
                </div>
              )}
              {tgSelected.length > 0 && (
                <Button size="sm" onClick={tgEnableSelected} disabled={!!tgBusy}>Подключить выбранные ({tgSelected.length})</Button>
              )}
              {tgChannels.length ? tgChannels.map((ch) => (
                <div key={ch.channel} className="rounded-md border p-2.5 text-[12px]">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-2">
                      <Badge variant={ch.enabled ? (ch.last_status === "error" ? "danger" : "success") : "default"}>
                        {ch.enabled ? (ch.last_status || "ожидает") : "выключен"}
                      </Badge>
                      <b className="truncate">@{ch.channel}</b>
                      <span className="text-[11px] text-muted-foreground">{ch.category}</span>
                    </div>
                    <div className="flex gap-1.5">
                      <Button size="sm" variant="ghost" onClick={() => tgAction("toggle", ch.channel, !ch.enabled)} disabled={!!busy}>{ch.enabled ? "Выключить" : "Включить"}</Button>
                      <Button size="sm" variant="ghost" onClick={() => tgAction("remove", ch.channel)} disabled={!!busy}>Удалить</Button>
                    </div>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-x-3 text-[11px] text-muted-foreground">
                    <span>постов: {ch.posts}</span><span>извлечено: {ch.vacancies}</span>
                    <span>в каталоге: {ch.jobs}</span><span>прошли: {ch.eligible}</span>
                    {ch.last_post_at && <span>последний пост: {dt(ch.last_post_at)}</span>}
                    {ch.quality ? <span>качество: {Math.round(ch.quality)}</span> : null}
                    <span>обновлён: {ch.last_run_at ? dt(ch.last_run_at) : "—"}</span>
                    {ch.last_error && <span className="text-destructive">{ch.last_error}</span>}
                  </div>
                </div>
              )) : <div className="text-[12px] text-muted-foreground">Каналы ещё не подключены.</div>}
            </CardContent></Card>
            <Card className="lg:col-span-2"><CardHeader className="flex-row items-center justify-between"><CardTitle>HeadHunter</CardTitle><div className="flex gap-2"><Button size="sm" asChild><a href="/autopilot">Подключить аккаунт</a></Button><Button size="sm" variant="outline" onClick={hhRefresh} disabled={!!tgBusy}>Проверить снова</Button></div></CardHeader><CardContent className="space-y-2 text-[12px]">
              {hh ? (
                <>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={hh.direct === "working" ? "success" : hh.direct === "blocked" ? "danger" : hh.direct === "unavailable" ? "warning" : "default"}>
                      прямой API: {hh.direct}
                    </Badge>
                    {hh.tokens?.HH_ACCESS_TOKEN ? <Badge variant="primary">OAuth token задан</Badge> : <Badge>OAuth token не задан</Badge>}
                  </div>
                  {hh.reason && <div className="text-muted-foreground">{hh.reason}</div>}
                  {hh.diagnostic?.request_id && <div className="text-muted-foreground">request_id: {hh.diagnostic.request_id}</div>}
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                    <div className="rounded-md border p-2"><div className="text-muted-foreground">вакансий с hh.ru URL</div><b>{hh.indirect?.with_hh_url ?? 0}</b></div>
                    <div className="rounded-md border p-2"><div className="text-muted-foreground">с полным описанием</div><b>{hh.indirect?.detailed ?? 0}</b></div>
                    <div className="rounded-md border p-2"><div className="text-muted-foreground">обогащено</div><b>{hh.indirect?.enriched ?? 0}</b></div>
                    <div className="rounded-md border p-2"><div className="text-muted-foreground">подходящих</div><b>{hh.indirect?.eligible ?? 0}</b></div>
                  </div>
                  <div className="text-[11px] text-muted-foreground">{hh.oauth_instructions}</div>
                  <div className="flex gap-2">
                    <Button size="sm" variant="outline" asChild><a href="https://hh.ru/" target="_blank" rel="noopener noreferrer">Открыть hh.ru</a></Button>
                    <Button size="sm" variant="ghost" asChild><a href="https://dev.hh.ru/" target="_blank" rel="noopener noreferrer">Регистрация приложения</a></Button>
                  </div>
                </>
              ) : <div className="text-muted-foreground">Проверка ещё не выполнялась.</div>}
            </CardContent></Card>
            <Card className="lg:col-span-2"><CardHeader><CardTitle>Автономный поиск и расписание</CardTitle></CardHeader><CardContent className="space-y-2 text-[12px]">
              {autoCfg ? (
                <>
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-4">
                    <label>Лёгкое обновление, мин<Input type="number" value={autoCfg.light_minutes ?? 30} onChange={(e) => setAutoCfg({ ...autoCfg, light_minutes: Number(e.target.value) })} /></label>
                    <label>Полное обновление, ч<Input type="number" value={autoCfg.full_hours ?? 6} onChange={(e) => setAutoCfg({ ...autoCfg, full_hours: Number(e.target.value) })} /></label>
                    <label>Discovery, ч<Input type="number" value={autoCfg.discovery_hours ?? 24} onChange={(e) => setAutoCfg({ ...autoCfg, discovery_hours: Number(e.target.value) })} /></label>
                    <label>Стартапы, ч<Input type="number" value={autoCfg.startups_hours ?? 24} onChange={(e) => setAutoCfg({ ...autoCfg, startups_hours: Number(e.target.value) })} /></label>
                  </div>
                  <div className="flex flex-wrap gap-4">
                    <label className="flex items-center gap-2"><input type="checkbox" checked={!!autoCfg.enabled} onChange={(e) => setAutoCfg({ ...autoCfg, enabled: e.target.checked })} />Автопоиск включён</label>
                    <label className="flex items-center gap-2"><input type="checkbox" checked={!!autoCfg.auto_enable_channels} onChange={(e) => setAutoCfg({ ...autoCfg, auto_enable_channels: e.target.checked })} />Автоподключать проверенные каналы</label>
                    <label className="flex items-center gap-2"><input type="checkbox" checked={autoCfg.prepare_best !== false} onChange={(e) => setAutoCfg({ ...autoCfg, prepare_best: e.target.checked })} />Готовить лучшие отклики</label>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button size="sm" onClick={saveAutomation} disabled={!!busy}>Сохранить расписание</Button>
                    <span className="text-[11px] text-muted-foreground">Windows Task Scheduler: каждые 30 минут; режим выбирает jobhunter.py auto.</span>
                  </div>
                </>
              ) : <div className="text-muted-foreground">Настройки ещё не загружены.</div>}
            </CardContent></Card>
          </div>
        </TabsContent>

        <TabsContent value="ai">
          <Card className="mb-4">
            <CardHeader>
              <CardTitle className="flex items-center gap-2"><Sparkles className="h-4 w-4" />Генерация писем</CardTitle>
              <div className="text-[12px] text-muted-foreground">Локальная Qwen бесплатна; AITUNNEL подключается только по правилам и бюджету.</div>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="grid gap-3 sm:grid-cols-3">
                <label className="text-[12px]">Провайдер
                  <Select className="mt-1" value={qw.mode || "auto"} onChange={(e) => saveQwenProvider(e.target.value)} disabled={busy === "qwen-provider"}>
                    <option value="auto">Автоматически</option>
                    <option value="local_only">Локальная Qwen</option>
                    <option value="cloud_only">Qwen через AITUNNEL</option>
                  </Select>
                </label>
                <div className="rounded-lg border p-3 text-[12px]">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-muted-foreground">Локальная Qwen</span>
                    <Badge variant={qwLocal.available ? "success" : "warning"}>{qwLocal.available ? "подключена" : "недоступна"}</Badge>
                  </div>
                  <div className="mt-1 truncate font-medium">{qwLocal.model || "qwen3.5:4b"}</div>
                  <div className="text-[11px] text-muted-foreground">{qwLocal.available ? "Ollama · без оплаты" : qwLocal.reason || "Ollama не запущен"}</div>
                </div>
                <div className="rounded-lg border p-3 text-[12px]">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-muted-foreground">AITUNNEL</span>
                    <Badge variant={qwCloud.available ? "success" : "warning"}>{qwCloud.available ? "готов" : "недоступен"}</Badge>
                  </div>
                  <div className="mt-1 truncate font-medium">{qwCloud.models?.cheap || "Qwen"}</div>
                  <div className="text-[11px] text-muted-foreground">{qwCloud.available ? `ключ: ${qwCloud.key_source === "dpapi" ? "DPAPI" : "переменная окружения"}` : qwCloud.reason || "ключ не задан"}</div>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" onClick={testQwen} disabled={busy === "qwen-test"}>
                  {busy === "qwen-test" ? <RefreshCw className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                  Проверить генерацию
                </Button>
                <label className="flex items-center gap-1.5 text-[11.5px] text-muted-foreground">
                  <input type="checkbox" checked={s.privacy?.allow_cloud_llm !== false}
                         onChange={(e) => saveCloudPrivacy(e.target.checked)}
                         disabled={busy === "qwen-privacy"} />
                  Разрешить AITUNNEL получать обезличенные факты
                </label>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[11.5px] text-muted-foreground">
                  {qwSpend.budget_rub
                    ? `Расход: ${Number(qwSpend.month_rub || 0).toFixed(2)} / ${Number(qwSpend.budget_rub || 0).toFixed(2)} ₽ за месяц · сегодня ${Number(qwSpend.today_rub || 0).toFixed(2)} ₽`
                    : "Платных запросов нет: AITUNNEL не подключён"}
                </span>
              </div>
              {qwenTest && (
                <div className="rounded-lg border bg-muted/30 p-3 text-[12px]">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={qwenTest.ok ? "success" : "warning"}>{qwenTest.ok ? "валидно" : "требует проверки"}</Badge>
                    <span className="text-muted-foreground">Qwen · {qwenTest.provider === "cloud" ? "AITUNNEL" : "локально"} · {qwenTest.model} · {qwenTest.seconds} с</span>
                    {qwenTest.cost_rub > 0 && <span className="text-muted-foreground">{qwenTest.cost_rub.toFixed(3)} ₽{qwenTest.estimated ? " (оценка)" : ""}</span>}
                  </div>
                  <div className="mt-2 line-clamp-4 whitespace-pre-wrap text-[11.5px] leading-relaxed text-muted-foreground">{qwenTest.preview}</div>
                </div>
              )}
              <details className="rounded-lg border p-3">
                <summary className="cursor-pointer text-[12px] font-medium text-muted-foreground hover:text-foreground">Лимиты и модели</summary>
                <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  <label className="text-[11.5px] text-muted-foreground">Месячный бюджет, ₽
                    <Input type="number" className="mt-1" value={qwenForm.monthly} onChange={(e) => setQwenForm({ ...qwenForm, monthly: e.target.value })} placeholder="270" />
                  </label>
                  <label className="text-[11.5px] text-muted-foreground">Максимум за письмо, ₽
                    <Input type="number" step="0.05" className="mt-1" value={qwenForm.letter} onChange={(e) => setQwenForm({ ...qwenForm, letter: e.target.value })} />
                  </label>
                  <label className="text-[11.5px] text-muted-foreground">Максимум за сутки, ₽
                    <Input type="number" step="1" className="mt-1" value={qwenForm.daily} onChange={(e) => setQwenForm({ ...qwenForm, daily: e.target.value })} />
                  </label>
                  <label className="text-[11.5px] text-muted-foreground">Qwen для массовых писем
                    <Input className="mt-1 font-mono text-[11.5px]" value={qwenForm.cheap} onChange={(e) => setQwenForm({ ...qwenForm, cheap: e.target.value })} />
                  </label>
                  <label className="text-[11.5px] text-muted-foreground">Qwen для сложных вакансий
                    <Input className="mt-1 font-mono text-[11.5px]" value={qwenForm.quality} onChange={(e) => setQwenForm({ ...qwenForm, quality: e.target.value })} />
                  </label>
                  <div className="flex items-end gap-2">
                    <label className="flex items-center gap-1.5 text-[11.5px] text-muted-foreground">
                      <input type="checkbox" checked={!!qwenForm.fallback} onChange={(e) => setQwenForm({ ...qwenForm, fallback: e.target.checked })} />
                      Разрешить fallback
                    </label>
                    <Button size="sm" variant="outline" onClick={saveQwenLimits} disabled={busy === "qwen-limits"}>Сохранить</Button>
                  </div>
                </div>
                <p className="mt-2 text-[11px] text-muted-foreground">Модели берутся из актуального каталога AITUNNEL; при превышении лимита используется локальная Qwen.</p>
              </details>
              <details open={qwenKeyOpen} onToggle={(e) => setQwenKeyOpen((e.currentTarget as HTMLDetailsElement).open)} className="rounded-lg border p-3">
                <summary className="cursor-pointer text-[12px] font-medium text-muted-foreground hover:text-foreground">
                  <KeyRound className="mr-1 inline h-3.5 w-3.5" />Ключ AITUNNEL
                </summary>
                <p className="mt-2 text-[11.5px] text-muted-foreground">Хранится локально в Windows DPAPI, не попадает в Git, SQLite, логи и интерфейс. Не отправляйте ключ в чат.</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  <Input type={showQwenKey ? "text" : "password"} className="max-w-[320px]" value={qwenKey} onChange={(e) => setQwenKey(e.target.value)} placeholder="вставьте ключ один раз" autoComplete="off" />
                  <Button size="sm" variant="outline" onClick={() => setShowQwenKey((v) => !v)}>{showQwenKey ? "Скрыть" : "Покивань"}</Button>
                  <Button size="sm" onClick={saveQwenKey} disabled={!qwenKey.trim() || busy === "qwen-key"}>Сохранить ключ</Button>
                </div>
              </details>
            </CardContent>
          </Card>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <Card><CardHeader><CardTitle className="flex items-center gap-2"><Cpu className="h-4 w-4" />Модель</CardTitle></CardHeader><CardContent className="space-y-1.5 text-[12.5px]">
              <div className="flex justify-between"><span className="text-muted-foreground">Провайдер</span><b>{ai.provider || "—"}</b></div>
              <div className="flex justify-between"><span className="text-muted-foreground">Модель</span><b className="max-w-[180px] truncate text-right">{ai.model || "локальные шаблоны"}</b></div>
              <div className="flex justify-between"><span className="text-muted-foreground">Каталог</span><b>{ai.models_in_catalog ?? "—"}</b></div>
              <Button size="sm" variant="outline" className="mt-2" onClick={() => run("catalog", () => apiPost("/api/model/refresh"), "Каталог обновлён")}><RefreshCw className="h-3.5 w-3.5" />Обновить каталог</Button>
            </CardContent></Card>
            <Card><CardHeader><CardTitle className="flex items-center gap-2"><Wallet className="h-4 w-4" />Расходы</CardTitle></CardHeader><CardContent className="space-y-1.5 text-[12.5px]">
              <div className="text-2xl font-semibold">{spend.toFixed(2)} {currency}</div>
              <div className="text-muted-foreground">Бюджет: {ai.budget_rub ?? ai.budget_usd} {currency}</div>
              <div className="text-[11.5px] text-muted-foreground">Кэш и точные cost_rub учитываются в ledger.</div>
              <Button size="sm" variant="outline" className="mt-2" asChild><a href="/api/export/analytics.json"><Download className="h-3.5 w-3.5" />Экспорт</a></Button>
            </CardContent></Card>
            <Card><CardHeader><CardTitle className="flex items-center gap-2"><Database className="h-4 w-4" />Память</CardTitle></CardHeader><CardContent className="space-y-1.5 text-[12.5px]">
              <div className="flex justify-between"><span className="text-muted-foreground">Документов</span><b>{data.memory.documents}</b></div>
              <div className="flex justify-between"><span className="text-muted-foreground">Векторов</span><b>{data.memory.vectors}</b></div>
              <div className="flex justify-between"><span className="text-muted-foreground">Модель</span><b>{data.memory.embedding?.model || data.memory.embedding_model}</b></div>
              <Button size="sm" variant="outline" className="mt-2" onClick={() => run("memory", () => apiPost("/api/memory/rebuild"), "Память переиндексирована")}>Переиндексировать</Button>
            </CardContent></Card>
          </div>
        </TabsContent>

        <TabsContent value="notifications">
          <Card><CardHeader><CardTitle>Уведомления</CardTitle><div className="text-[12px] text-muted-foreground">Короткие сообщения о том, что требует вашего внимания.</div></CardHeader><CardContent className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3">
              <div className="min-w-0">
                <div className="text-[13px] font-medium">Telegram-уведомления</div>
                <div className="text-[11.5px] text-muted-foreground">Приглашения, вопросы работодателей и сбои подключения. Токен и чат задаются в системе и здесь не показываются.</div>
              </div>
              <label className="flex items-center gap-2 text-[12px]">
                <input type="checkbox" checked={!!s.notifications?.telegram?.enabled} onChange={(e) => saveNotifications(e.target.checked)} disabled={busy === "notifications"} />
                {s.notifications?.telegram?.enabled ? "включены" : "выключены"}
              </label>
            </div>
            <p className="text-[11.5px] text-muted-foreground">Письма и сообщения работодателям автоматически не отправляются — только после вашего подтверждения.</p>
          </CardContent></Card>
        </TabsContent>

        <TabsContent value="diagnostics">
          <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-2">
            <Card><CardHeader><CardTitle className="flex items-center gap-2"><Activity className="h-4 w-4" />Калибровка</CardTitle></CardHeader><CardContent className="space-y-2 text-[12.5px]">
              <div>{data.calibration?.trained ? `N=${data.calibration.samples}, Brier=${data.calibration.brier}, AUC=${data.calibration.auc}` : data.calibration?.reason || "не обучена"}</div>
              <Button size="sm" variant="outline" onClick={() => run("calibrate", () => apiPost("/api/calibrate"), "Калибровка обновлена")}>Обучить</Button>
            </CardContent></Card>
            <Card><CardHeader><CardTitle>Экспорт</CardTitle></CardHeader><CardContent className="flex flex-wrap gap-2"><Button size="sm" variant="outline" asChild><a href="/api/export/jobs.csv">Вакансии CSV</a></Button><Button size="sm" variant="outline" asChild><a href="/api/export/queue.csv">Очередь CSV</a></Button><Button size="sm" variant="outline" asChild><a href="/api/export/analytics.json">Аналитика JSON</a></Button></CardContent></Card>
          </div>
          <details className="mt-4 rounded-xl border bg-card p-4">
            <summary className="cursor-pointer text-[13px] font-medium text-muted-foreground hover:text-foreground">Покивань JSON настроек</summary>
            <div className="mt-3">
              <Textarea value={settingsJson} onChange={(e) => setSettingsJson(e.target.value)} className="min-h-[320px] font-mono text-[11.5px]" />
              <Button className="mt-2" onClick={saveSettings} disabled={busy === "settings"}>Сохранить JSON</Button>
              <p className="mt-2 text-[11px] text-muted-foreground">Секреты здесь не хранятся: ключи задаются только в окружении.</p>
            </div>
          </details>
        </TabsContent>
      </Tabs>
    </div>
  );
}
