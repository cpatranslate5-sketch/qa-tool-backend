import { useState } from "react";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

interface Finding {
  type: string;
  severity: "low" | "medium" | "high";
  message: string;
}

const CHECK_OPTIONS: { key: string; label: string }[] = [
  { key: "numbers", label: "Числа/даты" },
  { key: "placeholders", label: "Плейсхолдеры/теги" },
  { key: "glossary", label: "Глоссарий" },
  { key: "register", label: "Регистр (ты/вы)" },
  { key: "typo", label: "Опечатки/искажения" },
];

const SEVERITY_LABEL: Record<string, string> = { high: "Важно", medium: "Средне", low: "Мелочь" };

export default function App() {
  const [source, setSource] = useState("");
  const [translation, setTranslation] = useState("");
  const [glossary, setGlossary] = useState("");
  const [checks, setChecks] = useState<string[]>(CHECK_OPTIONS.map(c => c.key));
  const [findings, setFindings] = useState<Finding[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  function toggleCheck(key: string) {
    setChecks(prev => prev.includes(key) ? prev.filter(c => c !== key) : [...prev, key]);
  }

  async function runCheck() {
    if (!source.trim() || !translation.trim()) return;
    setLoading(true);
    setError("");
    setFindings(null);
    try {
      const res = await fetch(`${API_URL}/check`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, translation, glossary, checks }),
      });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      setFindings(data.findings);
    } catch {
      setError("Не удалось связаться с сервером проверки.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="page">
      <h1>QA переводов</h1>

      <div className="two-col">
        <div className="col">
          <label>Исходный текст</label>
          <textarea value={source} onChange={e => setSource(e.target.value)} rows={12} placeholder="Вставьте исходный текст…" />
        </div>
        <div className="col">
          <label>Перевод</label>
          <textarea value={translation} onChange={e => setTranslation(e.target.value)} rows={12} placeholder="Вставьте перевод…" />
        </div>
      </div>

      <label>Глоссарий (необязательно)</label>
      <textarea value={glossary} onChange={e => setGlossary(e.target.value)} rows={3}
        placeholder="Например: term1 → перевод1, term2 → перевод2" />

      <div className="checks-row">
        {CHECK_OPTIONS.map(c => (
          <label key={c.key} className="check-chip">
            <input type="checkbox" checked={checks.includes(c.key)} onChange={() => toggleCheck(c.key)} />
            {c.label}
          </label>
        ))}
      </div>

      <button onClick={runCheck} disabled={loading || !source.trim() || !translation.trim()}>
        {loading ? "Проверяю…" : "Проверить"}
      </button>

      {error && <div className="error-box">{error}</div>}

      {findings !== null && (
        <div className="results">
          <h2>Результат</h2>
          {findings.length === 0 && <div className="muted">Проблем не найдено.</div>}
          {findings.map((f, i) => (
            <div key={i} className={`finding finding-${f.severity}`}>
              <span className="finding-severity">{SEVERITY_LABEL[f.severity] || f.severity}</span>
              <span className="finding-type">{f.type}</span>
              <div className="finding-message">{f.message}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
