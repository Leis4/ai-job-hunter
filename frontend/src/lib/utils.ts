import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function letterOrigin(source?: string | null) {
  if (!source || !source.startsWith("qwen:")) return "";
  const provider = source.split(":")[1] || "local";
  return `Создано Qwen · ${provider === "cloud" ? "AITUNNEL" : "локально"}`;
}

export function money(low?: number | null, high?: number | null, currency = "RUB") {  if (!low && !high) return "—";
  const symbol = currency === "USD" ? "$" : currency === "EUR" ? "€" : "₽";
  const fmt = (n: number) => new Intl.NumberFormat("ru-RU").format(n);
  if (low && high && low !== high) return `${fmt(low)} – ${fmt(high)} ${symbol}`;
  return `${fmt(low || high || 0)} ${symbol}`;
}

export function pct(value?: number | null) {
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

export function dt(value?: string | null) {
  if (!value) return "—";
  const d = new Date(value.replace(" ", "T"));
  if (Number.isNaN(d.getTime())) return value.slice(0, 16);
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function day(value?: string | null) {
  if (!value) return "—";
  const d = new Date(value.replace(" ", "T"));
  return Number.isNaN(d.getTime()) ? value.slice(0, 10) : d.toLocaleDateString("ru-RU", { day: "2-digit", month: "long" });
}

export const STATUS_LABELS: Record<string, string> = {
  new: "Новая", draft: "Черновик", queued: "В очереди", approved: "Одобрена",
  submitted: "Отправлена", viewed: "Просмотрена", replied: "Ответ", invited: "Приглашение",
  offer: "Оффер", rejected: "Отказ", archived: "Архив", skipped: "Пропущена",
};

export const QUEUE_LABELS: Record<string, string> = {
  queued: "Ожидает подтверждения", manual_review: "Для ручной проверки", approved: "Одобрено",
  opening_application: "Открытие формы", form_filling: "Заполнение формы",
  requires_user_action: "Нужно действие", sending: "Отправляется", submitted: "Отправлено",
  submission_unverified: "Отправка не подтверждена", exported: "Выгружено", failed: "Ошибка",
  blocked: "Заблокировано", cancelled: "Отменено",
  ready_to_review: "Проверить и одобрить", viewed: "Просмотрено", interview: "Интервью",
  offer: "Оффер", rejected: "Отказ", withdrawn: "Отозвано", unknown: "Результат неизвестен",
};

export const MODE_LABELS: Record<string, string> = {
  remote: "Удалённо", hybrid: "Гибрид", office: "Офис",
};

export const SOURCE_LABELS: Record<string, string> = {
  trudvsem: "Работа России",
  habr: "Хабр Карьера",
  vk_career: "Карьера VK",
  mts: "Карьера МТС",
  telegram: "Telegram",
  ats: "ATS работодателей",
  tbank: "Карьера Т-Банка",
  superjob: "SuperJob",
  hh_api: "hh.ru API",
  feed: "RSS/фиды",
  demo: "демо",
  "user-import": "импорт",
};

export const FACTOR_LABELS: Record<string, string> = {  role: "Роль и заголовок",
  skills: "Совпадение навыков",
  text: "Текст вакансии",
  experience: "Опыт",
  work_mode: "Формат работы",
  location: "Локация",
  salary: "Зарплата",
  freshness: "Свежесть вакансии",
  education: "Образование",
  license: "Лицензия/допуск",
};
