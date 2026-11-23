import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Filter, RotateCcw, Search, Sparkles, ChevronDown } from "lucide-react";
import { apiGet } from "@/lib/api";
import { Job } from "@/lib/types";
import { PageHeader, EmptyState, ErrorState, LoadingState } from "@/components/PageChrome";
import { JobCard } from "@/components/JobCard";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/input";

type Filters = {
  view: string; q: string; role: string; skill: string; source: string; status: string; mode: string;
  min_score: string; max_experience: string; min_salary: string; sort: string;
  eligible: boolean; favorite: boolean; all_required: boolean;
  exclude_company: string; exclude_words: string;
};
const EMPTY: Filters = {
  view: "prospects", q: "", role: "", skill: "", source: "", status: "", mode: "", min_score: "",
  max_experience: "", min_salary: "", sort: "score", eligible: false, favorite: false,
  all_required: false, exclude_company: "", exclude_words: "",
};
const QUICK = [
  ["prospects", "Все"],
  ["matched", "Подходящие"],
  ["interns", "Стажировки"],
  ["remote", "Удалённо"],
  ["saved", "Сохранённые"],
] as const;

export default function VacanciesPage() {
  const [searchParams] = useSearchParams();
  const [filters, setFilters] = useState<Filters>(() => ({ ...EMPTY,
    view: ["all", "matched", "prospects", "interns"].includes(searchParams.get("view") || "")
      ? (searchParams.get("view") as string) : EMPTY.view,
  }));
  const [items, setItems] = useState<Job[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const pageSize = 30;

  const query = useMemo(() => {
    const p = new URLSearchParams();
    const effective = { ...filters };
    if (effective.view === "remote") { effective.mode = "remote"; effective.view = "all"; }
    if (effective.view === "saved") { effective.favorite = true; effective.view = "all"; }
    Object.entries(effective).forEach(([k, v]) => {
      if (typeof v === "boolean" ? v : v) p.set(k, typeof v === "boolean" ? "1" : String(v));
    });
    p.set("page", String(page)); p.set("size", String(pageSize));
    return p.toString();
  }, [filters, page]);

  async function load() {
    setLoading(true); setError("");
    try {
      const data = await apiGet<{ items: Job[]; total: number }>("/api/jobs?" + query);
      setItems(data.items); setTotal(data.total);
    } catch (e) { setError((e as Error).message); } finally { setLoading(false); }
  }
  useEffect(() => { const id = setTimeout(load, 180); return () => clearTimeout(id); }, [query]);

  function set<K extends keyof Filters>(key: K, value: Filters[K]) { setPage(0); setFilters((f) => ({ ...f, [key]: value })); }
  function reset() { setFilters(EMPTY); setPage(0); }

  return (
    <div className="animate-rise">
      <PageHeader eyebrow="Вакансии" title="Найденные вакансии"
        description="Сначала подходящие, потом всё остальное. Оценка соответствия — не вероятность приглашения."
        actions={<div className="flex gap-2"><Button size="sm" variant="outline" asChild><Link to="/startups"><Sparkles className="h-3.5 w-3.5" />Стартапы</Link></Button><Button size="sm" variant="ghost" onClick={reset}><RotateCcw className="h-3.5 w-3.5" />Сбросить</Button></div>} />

      <div className="mb-4 space-y-3">
        <div className="flex flex-wrap items-center gap-1.5">
          {QUICK.map(([value, label]) => (
            <Button key={value} size="sm" variant={filters.view === value ? "default" : "outline"} onClick={() => set("view", value)}>{label}</Button>
          ))}
          <div className="relative ml-auto min-w-[220px] flex-1 sm:max-w-[320px]">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input className="bg-card pl-8" placeholder="Должность, компания, навык" value={filters.q} onChange={(e) => set("q", e.target.value)} />
          </div>
        </div>

        <details className="rounded-xl border bg-card p-3">
          <summary className="flex cursor-pointer list-none items-center gap-2 text-[12.5px] font-medium text-muted-foreground hover:text-foreground"><Filter className="h-3.5 w-3.5" />Фильтры<ChevronDown className="h-3.5 w-3.5" /></summary>
          <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <Input placeholder="Роль" value={filters.role} onChange={(e) => set("role", e.target.value)} />
            <Input placeholder="Навык" value={filters.skill} onChange={(e) => set("skill", e.target.value)} />
            <Select value={filters.mode} onChange={(e) => set("mode", e.target.value)}><option value="">Любой формат</option><option value="remote">Удалённо</option><option value="hybrid">Гибрид</option><option value="office">Офис</option></Select>
            <Select value={filters.source} onChange={(e) => set("source", e.target.value)}><option value="">Все источники</option><option value="telegram">Telegram</option><option value="habr">Хабр Карьера</option><option value="trudvsem">Работа России</option><option value="mts">МТС</option><option value="vk_career">VK</option><option value="ats">ATS</option><option value="hh_api">hh.ru</option></Select>
            <Select value={filters.max_experience} onChange={(e) => set("max_experience", e.target.value)}><option value="">Опыт: любой</option><option value="0">Без опыта</option><option value="1">До 1 года</option><option value="2">До 2 лет</option><option value="3">До 3 лет</option></Select>
            <Select value={filters.min_salary} onChange={(e) => set("min_salary", e.target.value)}><option value="">Зарплата: любая</option><option value="50000">от 50 тыс.</option><option value="80000">от 80 тыс.</option><option value="100000">от 100 тыс.</option><option value="150000">от 150 тыс.</option></Select>
            <Select value={filters.min_score} onChange={(e) => set("min_score", e.target.value)}><option value="">Соответствие: любое</option><option value="50">от 50</option><option value="65">от 65</option><option value="80">от 80</option></Select>
            <Select value={filters.sort} onChange={(e) => set("sort", e.target.value)}><option value="score">Сначала подходящие</option><option value="date">Сначала новые</option><option value="salary">Сначала с зарплатой</option></Select>
          </div>
          <div className="mt-2 flex flex-wrap gap-4 text-[12px] text-muted-foreground">
            <label className="flex items-center gap-1.5"><input type="checkbox" checked={filters.favorite} onChange={(e) => set("favorite", e.target.checked)} />Только сохранённые</label>
            <label className="flex items-center gap-1.5"><input type="checkbox" checked={filters.eligible} onChange={(e) => set("eligible", e.target.checked)} />Только подходящие</label>
            <Input className="max-w-[240px]" placeholder="Исключить компанию" value={filters.exclude_company} onChange={(e) => set("exclude_company", e.target.value)} />
          </div>
        </details>
      </div>

      <div className="mb-2 text-[11.5px] text-muted-foreground">{total} вакансий · страница {page + 1}</div>
      {error && <ErrorState error={error} onRetry={load} />}
      {loading ? <LoadingState rows={6} /> : (
        <Card className="overflow-hidden p-0">
          {items.length ? items.map((job) => <JobCard key={job.id} job={job} onChanged={load} />) : (
            <EmptyState icon={<Search className="h-5 w-5" />} title="Пока ничего не найдено" description="Поиск продолжится автоматически. Попробуйте снять фильтры." />
          )}
        </Card>
      )}
      <div className="mt-4 flex items-center justify-between">
        <Button variant="outline" size="sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>Назад</Button>
        <span className="text-[11.5px] text-muted-foreground">{total ? `${page * pageSize + 1}–${Math.min(total, (page + 1) * pageSize)} из ${total}` : "0"}</span>
        <Button variant="outline" size="sm" disabled={(page + 1) * pageSize >= total} onClick={() => setPage((p) => p + 1)}>Вперёд</Button>
      </div>
    </div>
  );
}
