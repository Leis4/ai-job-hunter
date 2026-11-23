import { NavLink, useLocation } from "react-router-dom";
import { useEffect, useState } from "react";
import {
  BriefcaseBusiness, LayoutDashboard, Menu, Moon, Settings, Sun, X, Sparkles, Send, Bot,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useTheme } from "@/lib/useTheme";

const NAV = [
  { to: "/", label: "Главная", icon: LayoutDashboard },
  { to: "/vacancies", label: "Вакансии", icon: BriefcaseBusiness },
  { to: "/applications", label: "Отклики", icon: Send },
  { to: "/autopilot", label: "Автопилот", icon: Bot },
  { to: "/settings", label: "Настройки", icon: Settings },
];

function NavItems({ onNavigate, mobile = false }: { onNavigate?: () => void; mobile?: boolean }) {
  return (
    <nav className={mobile ? "grid grid-cols-5 gap-1" : "space-y-0.5"}>
      {NAV.map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          end={to === "/"}
          onClick={onNavigate}
          className={({ isActive }) => cn(
            "flex items-center gap-2.5 rounded-lg px-3 text-[13.5px] font-medium text-muted-foreground transition-colors",
            mobile ? "h-14 flex-col justify-center gap-1 px-1 text-[10.5px]" : "h-9",
            "hover:bg-accent hover:text-foreground",
            isActive && "bg-primary/10 text-primary",
          )}
        >
          <Icon className={mobile ? "h-[18px] w-[18px]" : "h-4 w-4"} strokeWidth={1.9} />
          <span>{label}</span>
        </NavLink>
      ))}
    </nav>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const { dark, toggle } = useTheme();
  const [open, setOpen] = useState(false);
  const location = useLocation();

  useEffect(() => setOpen(false), [location.pathname]);
  useEffect(() => {
    document.body.style.overflow = open ? "hidden" : "";
    return () => { document.body.style.overflow = ""; };
  }, [open]);

  return (
    <div className="min-h-screen overflow-x-clip bg-background">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-[236px] flex-col border-r bg-card/70 px-3 py-4 backdrop-blur lg:flex">
        <NavLink to="/" className="flex items-center gap-2.5 rounded-lg px-2 py-1.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground shadow-sm">
            <Sparkles className="h-4 w-4" />
          </span>
          <span>
            <span className="block text-[14px] font-semibold leading-tight">AI Job Hunter</span>
            <span className="block text-[10.5px] text-muted-foreground">поиск работы без рутины</span>
          </span>
        </NavLink>
        <div className="mt-6 flex-1">
          <div className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-[0.13em] text-muted-foreground">Рабочее пространство</div>
          <NavItems />
        </div>
        <div className="space-y-2">
          <NavLink to="/statistics" className="flex items-center justify-between rounded-lg border bg-muted/30 px-3 py-2 text-[12px] text-muted-foreground hover:text-foreground">
            <span>Аналитика</span><span className="text-[11px]">→</span>
          </NavLink>
          <button onClick={toggle} className="flex h-9 w-full items-center gap-2 rounded-lg px-3 text-[12.5px] font-medium text-muted-foreground hover:bg-accent hover:text-foreground">
            {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            {dark ? "Светлая тема" : "Тёмная тема"}
          </button>
        </div>
      </aside>

      <header className="sticky top-0 z-40 flex h-14 items-center gap-3 border-b bg-card/90 px-4 backdrop-blur lg:hidden">
        <button onClick={() => setOpen(true)} className="rounded-lg p-2 hover:bg-accent" aria-label="Открыть меню">
          <Menu className="h-5 w-5" />
        </button>
        <NavLink to="/" className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-primary" /><span className="text-[14px] font-semibold">AI Job Hunter</span>
        </NavLink>
        <div className="flex-1" />
        <button onClick={toggle} className="rounded-lg p-2 text-muted-foreground hover:bg-accent" aria-label="Сменить тему">
          {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </button>
      </header>

      {open && (
        <div className="fixed inset-0 z-50 lg:hidden">
          <div className="absolute inset-0 bg-black/40" onClick={() => setOpen(false)} />
          <div className="absolute inset-y-0 left-0 w-[280px] border-r bg-card p-4 shadow-xl">
            <div className="mb-6 flex items-center justify-between">
              <div className="flex items-center gap-2"><Sparkles className="h-5 w-5 text-primary" /><b>AI Job Hunter</b></div>
              <button onClick={() => setOpen(false)} className="rounded-lg p-2 hover:bg-accent" aria-label="Закрыть"><X className="h-4 w-4" /></button>
            </div>
            <NavItems onNavigate={() => setOpen(false)} />
            <NavLink to="/statistics" onClick={() => setOpen(false)} className="mt-4 flex items-center justify-between rounded-lg border px-3 py-2 text-[12.5px] text-muted-foreground">Аналитика <span>→</span></NavLink>
          </div>
        </div>
      )}

      <main className="mx-auto min-h-screen w-full max-w-[1180px] px-4 pb-24 pt-5 sm:px-6 lg:pb-14 lg:pl-[268px] lg:pr-8 lg:pt-8">
        {children}
      </main>

      <div className="fixed inset-x-0 bottom-0 z-30 border-t bg-card/95 px-2 pb-[max(env(safe-area-inset-bottom),4px)] backdrop-blur lg:hidden">
        <NavItems mobile />
      </div>
    </div>
  );
}
