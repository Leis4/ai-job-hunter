import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/AppShell";
import HomePage from "@/pages/Home";
import VacanciesPage from "@/pages/Vacancies";
import VacancyDetailPage from "@/pages/VacancyDetail";
import ApplicationsPage from "@/pages/Applications";
import ResumePage from "@/pages/ResumeStudio";
import SettingsPage from "@/pages/Settings";
import StartupsPage from "@/pages/Startups";
import StatisticsPage from "@/pages/Statistics";
import AutopilotPage from "@/pages/Autopilot";

export default function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/vacancies" element={<VacanciesPage />} />
        <Route path="/vacancies/:id" element={<VacancyDetailPage />} />
        <Route path="/applications" element={<ApplicationsPage />} />
        <Route path="/startups" element={<StartupsPage />} />
        <Route path="/statistics" element={<StatisticsPage />} />
        <Route path="/autopilot" element={<AutopilotPage />} />
        <Route path="/queue" element={<Navigate to="/applications" replace />} />
        <Route path="/resume" element={<ResumePage />} />
        <Route path="/onboarding" element={<Navigate to="/resume" replace />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AppShell>
  );
}
