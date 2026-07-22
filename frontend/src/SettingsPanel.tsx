import { useCallback, useEffect, useState } from "react";

import { api } from "./api";
import type {
  BackupInventory,
  CustomProvider,
  CustomProvidersPayload,
  LogMaintenanceStatus,
  SettingsField,
  SettingsPayload,
  WebUpdateCheck,
  WebUpdateStatus,
} from "./types";

function formatLocalDateTime(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${String(date.getFullYear()).padStart(4, "0")}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function PanelTitle({ code, title, meta }: { code: string; title: string; meta: string }) {
  return <div className="panel-title"><span>{code}</span><h2>{title}</h2><small>{meta}</small></div>;
}

export function WebUpdatePanel({
  busy,
  setBusy,
  setError,
}: {
  busy: string | null;
  setBusy: (value: string | null) => void;
  setError: (value: string | null) => void;
}) {
  const [status, setStatus] = useState<WebUpdateStatus | null>(null);
  const [checkResult, setCheckResult] = useState<WebUpdateCheck | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const refreshStatus = useCallback(async () => {
    const next = await api<WebUpdateStatus>("/api/update/status");
    setStatus(next);
    return next;
  }, []);

  useEffect(() => {
    refreshStatus().catch(() => undefined);
  }, [refreshStatus]);

  const check = useCallback(async () => {
    setBusy("update-check");
    setError(null);
    setConfirming(false);
    setNote("正在检查远端版本…");
    try {
      const next = await api<WebUpdateCheck>("/api/update/check", { method: "POST" });
      setCheckResult(next);
      setNote(null);
    } catch (reason) {
      setCheckResult(null);
      setNote(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [setBusy, setError]);

  const update = useCallback(async () => {
    setBusy("update");
    setError(null);
    setNote("正在启动安全更新…");
    try {
      await api<{ started: boolean }>("/api/update", { method: "POST" });
    } catch (reason) {
      setBusy(null);
      setConfirming(false);
      setNote(null);
      setError(reason instanceof Error ? reason.message : String(reason));
      return;
    }

    // The dedicated update service deliberately takes this backend offline.
    // Keep polling through the expected disconnect and read the root worker's
    // persisted terminal result after the updated or rolled-back service returns.
    for (let attempt = 0; attempt < 1500; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      try {
        const next = await refreshStatus();
        if (next.phase === "running" || next.phase === "idle") {
          setNote("更新中：正在备份、安装依赖、构建并执行健康检查…");
          continue;
        }
        if (next.phase === "completed") {
          setNote(`${next.message}，正在载入新版本…`);
          window.location.reload();
          return;
        }
        if (next.phase === "failed") {
          setBusy(null);
          setConfirming(false);
          setNote(null);
          setError(next.message);
          return;
        }
      } catch {
        setNote("更新中：后端暂时离线，等待服务恢复…");
      }
    }
    setBusy(null);
    setConfirming(false);
    setNote(null);
    setError("更新在 50 分钟内没有返回结果，请检查 candlepilot-update.service 日志。");
  }, [refreshStatus, setBusy, setError]);

  const commitRange = status?.from_commit && status.current_commit
    ? `${status.from_commit.slice(0, 12)} → ${status.current_commit.slice(0, 12)}`
    : null;

  return (
    <div className="settings-section web-update-section">
      <h4 className="account-subhead">软件更新</h4>
      <div className="settings-actions">
        <button
          className="compact"
          disabled={busy !== null || status === null || !status.supported || status.phase === "running"}
          onClick={check}
        >
          {busy === "update-check" ? "检查中…" : "检查更新"}
        </button>
        {!confirming ? (
          <button
            className="compact"
            disabled={busy !== null || status?.phase === "running" || !checkResult?.update_available}
            onClick={() => setConfirming(true)}
          >
            安装更新
          </button>
        ) : (
          <>
            <span className="history-warn">
              确认更新？必须先停止引擎、回测和试跑。服务会短暂离线；更新失败将自动回滚。
            </span>
            <button className="compact" disabled={busy !== null} onClick={update}>
              {busy === "update" ? "更新中…" : "确认更新"}
            </button>
            <button className="text-button" disabled={busy !== null} onClick={() => setConfirming(false)}>
              取消
            </button>
          </>
        )}
        {note && <span className="settings-saved">{note}</span>}
      </div>
      <small className="settings-hint">
        {status?.supported
          ? "调用 VPS 安装器的安全原地更新：仅接受 main 快进，保留 .env、数据库、行情、TLS 和模型登录；更新前备份，失败自动回滚。"
          : status?.message ?? "正在检查 VPS 更新能力…"}
      </small>
      {checkResult && (
        <div className={`update-check-result ${checkResult.update_available ? "available" : "current"}`}>
          <strong>{checkResult.message}</strong>
          <span>{checkResult.branch}</span>
          <span>{checkResult.current_commit.slice(0, 12)} → {checkResult.latest_commit.slice(0, 12)}</span>
          <span>{formatLocalDateTime(new Date(checkResult.checked_at))}</span>
        </div>
      )}
      {status && status.phase !== "idle" && (
        <div className={`update-result ${status.phase}`}>
          <strong>{status.message}</strong>
          {commitRange && <span>{commitRange}</span>}
          {status.backup && <span>备份：{status.backup}</span>}
          {status.finished_at && <span>{formatLocalDateTime(new Date(status.finished_at))}</span>}
        </div>
      )}
    </div>
  );
}

function formatStorageSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && value >= 1024; index += 1) {
    value /= 1024;
    unit = units[index];
  }
  return `${value >= 10 ? value.toFixed(1) : value.toFixed(2)} ${unit}`;
}

export function BackupPanel({
  busy,
  setBusy,
  setError,
}: {
  busy: string | null;
  setBusy: (value: string | null) => void;
  setError: (value: string | null) => void;
}) {
  const [inventory, setInventory] = useState<BackupInventory | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    const next = await api<BackupInventory>("/api/backups");
    setInventory(next);
    return next;
  }, []);

  useEffect(() => {
    load().catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, [load, setError]);

  const waitForResult = useCallback(async () => {
    for (let attempt = 0; attempt < 120; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      const next = await load();
      if (next.status.phase === "completed") return next;
      if (next.status.phase === "failed") throw new Error(next.status.message);
    }
    throw new Error("备份维护在 60 秒内没有返回结果，请检查更新服务日志。");
  }, [load]);

  const runAction = useCallback(async (path: string, busyKey: string) => {
    setWorking(true);
    setBusy(busyKey);
    setError(null);
    setNote("备份维护已排队…");
    try {
      await api<{ queued: boolean }>(path, { method: "POST" });
      const next = await waitForResult();
      const reclaimed = next.status.reclaimed_bytes;
      setNote(reclaimed === null
        ? next.status.message
        : `${next.status.message}，释放 ${formatStorageSize(reclaimed)}`);
      setConfirming(null);
    } catch (reason) {
      setNote(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setWorking(false);
      setBusy(null);
    }
  }, [setBusy, setError, waitForResult]);

  const blocked = busy !== null || working || inventory?.status.phase === "running";
  const total = inventory?.backups.reduce((sum, backup) => sum + backup.size_bytes, 0) ?? 0;

  return (
    <div className="settings-section web-update-section backup-section">
      <h4 className="account-subhead">服务器备份</h4>
      <div className="settings-actions">
        <button
          className="compact"
          disabled={blocked || inventory === null || !inventory.supported}
          onClick={() => runAction("/api/backups/refresh", "backup-refresh")}
        >
          {working && busy === "backup-refresh" ? "刷新中…" : "刷新备份清单"}
        </button>
        {inventory?.backups.length ? (
          <span className="settings-saved">{inventory.backups.length} 份 · {formatStorageSize(total)}</span>
        ) : null}
        {note && <span className="settings-saved">{note}</span>}
      </div>
      <small className="settings-hint">
        {inventory?.supported
          ? "只列出安装器创建的标准备份。最新一份始终保留；删除由受限的 root 维护服务执行且不可恢复。"
          : inventory?.status.message ?? "正在读取 VPS 备份清单…"}
      </small>
      {inventory?.backups.length === 0 && inventory.supported && (
        <div className="empty backup-empty">清单为空；刷新后显示现有备份。</div>
      )}
      {inventory && inventory.backups.length > 0 && (
        <div className="backup-list">
          {inventory.backups.map((backup) => (
            <div className="backup-row" key={backup.id}>
              <div>
                <strong>{formatLocalDateTime(new Date(backup.created_at))}</strong>
                <small>{backup.id} · {formatStorageSize(backup.size_bytes)}{backup.source_commit ? ` · ${backup.source_commit.slice(0, 12)}` : ""}</small>
              </div>
              <div className="backup-actions">
                {backup.protected ? (
                  <span className="backup-protected">最新 · 保留</span>
                ) : confirming === backup.id ? (
                  <>
                    <span className="history-warn">确认永久删除这份备份？</span>
                    <button className="compact danger" disabled={blocked} onClick={() => runAction(`/api/backups/${backup.id}/delete`, "backup-delete")}>确认删除</button>
                    <button className="text-button" disabled={blocked} onClick={() => setConfirming(null)}>取消</button>
                  </>
                ) : (
                  <button className="text-button danger-text" disabled={blocked} onClick={() => setConfirming(backup.id)}>删除</button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function LogMaintenancePanel({
  busy,
  setBusy,
  setError,
}: {
  busy: string | null;
  setBusy: (value: string | null) => void;
  setError: (value: string | null) => void;
}) {
  const [status, setStatus] = useState<LogMaintenanceStatus | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    const next = await api<LogMaintenanceStatus>("/api/logs");
    setStatus(next);
    return next;
  }, []);

  useEffect(() => {
    load().catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, [load, setError]);

  const clearLogs = useCallback(async () => {
    setBusy("clear-logs");
    setError(null);
    setNote("日志清理已排队…");
    try {
      await api<{ queued: boolean }>("/api/logs/clear", { method: "POST" });
      for (let attempt = 0; attempt < 180; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        let next: LogMaintenanceStatus;
        try {
          next = await load();
        } catch {
          setNote("服务正在切换专用日志，等待恢复…");
          continue;
        }
        if (next.phase === "running" || next.phase === "idle") {
          setNote("正在隔离并清理 CandlePilot 日志，服务可能短暂重连…");
          continue;
        }
        if (next.phase === "failed") throw new Error(next.message);
        const sizes = next.before_bytes !== null && next.after_bytes !== null
          ? `（${formatStorageSize(next.before_bytes)} → ${formatStorageSize(next.after_bytes)}）`
          : "";
        setNote(`${next.message}${sizes}`);
        setConfirming(false);
        return;
      }
      throw new Error("日志清理在 90 秒内没有返回结果，请检查更新服务日志。");
    } catch (reason) {
      setNote(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [load, setBusy, setError]);

  const blocked = busy !== null || status === null || !status.supported || status.phase === "running";
  return (
    <div className="settings-section web-update-section">
      <h4 className="account-subhead">CandlePilot 日志</h4>
      <div className="settings-actions">
        {!confirming ? (
          <button className="compact danger" disabled={blocked} onClick={() => setConfirming(true)}>
            清除日志
          </button>
        ) : (
          <>
            <span className="history-warn">确认永久清除 CandlePilot 专用日志？活动任务必须已停止，首次启用隔离时服务会短暂重启。</span>
            <button className="compact danger" disabled={busy !== null} onClick={clearLogs}>确认清除</button>
            <button className="text-button" disabled={busy !== null} onClick={() => setConfirming(false)}>取消</button>
          </>
        )}
        {note && <span className="settings-saved">{note}</span>}
      </div>
      <small className="settings-hint">
        {status?.supported
          ? "只清理 CandlePilot 的独立 systemd journal，不影响 SSH、Nginx 或其他服务；删除不可恢复。"
          : status?.message ?? "正在读取 VPS 日志管理状态…"}
      </small>
      {status?.finished_at && status.phase !== "running" && (
        <div className={`update-result ${status.phase}`}>
          <strong>{status.message}</strong>
          {status.before_bytes !== null && status.after_bytes !== null && (
            <span>{formatStorageSize(status.before_bytes)} → {formatStorageSize(status.after_bytes)}</span>
          )}
          <span>{formatLocalDateTime(new Date(status.finished_at))}</span>
        </div>
      )}
    </div>
  );
}

function RestartPanel({
  busy,
  setBusy,
  setError,
}: {
  busy: string | null;
  setBusy: (value: string | null) => void;
  setError: (value: string | null) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const restart = useCallback(async () => {
    setBusy("restart");
    setError(null);
    setNote("正在重启后端…");
    try {
      await api<{ restarting: boolean }>("/api/restart", { method: "POST" });
    } catch (reason) {
      setBusy(null);
      setConfirming(false);
      setNote(null);
      setError(reason instanceof Error ? reason.message : String(reason));
      return;
    }
    // The process is replaced, so poll until the new one answers, then reload
    // to pick up the fresh state.
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      try {
        const response = await fetch("/api/health/live", { cache: "no-store" });
        if (response.ok) {
          setNote("后端已重启，正在刷新…");
          window.location.reload();
          return;
        }
      } catch {
        // Expected while the old process is gone and the new one is binding.
      }
    }
    setBusy(null);
    setConfirming(false);
    setNote(null);
    setError("后端在 30 秒内没有恢复，请检查启动它的终端。");
  }, [setBusy, setError]);

  return (
    <div className="settings-section">
      <h4 className="account-subhead">重启后端</h4>
      <div className="settings-actions">
        {!confirming ? (
          <button className="compact" disabled={busy !== null} onClick={() => setConfirming(true)}>
            重启后端
          </button>
        ) : (
          <>
            <span className="history-warn">确认重启？引擎、回测、探测和调度任务必须已停止；重启期间页面会短暂断开。</span>
            <button className="compact" disabled={busy !== null} onClick={restart}>
              {busy === "restart" ? "重启中…" : "确认重启"}
            </button>
            <button className="text-button" disabled={busy !== null} onClick={() => setConfirming(false)}>
              取消
            </button>
          </>
        )}
        {note && <span className="settings-saved">{note}</span>}
      </div>
      <small className="settings-hint">
        用当前 .env 重新启动后端进程，让上面保存的设置生效。引擎、回测、探测、采集或调度任务运行中会被拒绝；
        由 .env 注入的旧值会被清掉，但你在 shell 里 export 的变量仍然优先。
      </small>
    </div>
  );
}

type ProviderDraft = CustomProvider & { api_key: string | null };

function CustomProvidersPanel({
  busy,
  setBusy,
  setError,
}: {
  busy: string | null;
  setBusy: (value: string | null) => void;
  setError: (value: string | null) => void;
}) {
  const [payload, setPayload] = useState<CustomProvidersPayload | null>(null);
  const [drafts, setDrafts] = useState<ProviderDraft[] | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [revealedKeys, setRevealedKeys] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const next = await api<CustomProvidersPayload>("/api/custom-providers");
      setPayload(next);
      setDrafts(null);
      setRevealedKeys({});
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [setError]);

  useEffect(() => { load(); }, [load]);

  // api_key null means "leave the stored key alone" — the frontend never holds it.
  const rows: ProviderDraft[] =
    drafts ?? (payload?.providers ?? []).map((p) => ({ ...p, api_key: null }));
  const dirty = drafts !== null;

  const update = (index: number, patch: Partial<ProviderDraft>) =>
    setDrafts(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));

  const revealKey = useCallback(async (providerId: string) => {
    setBusy(`reveal-key-${providerId}`);
    setError(null);
    try {
      const result = await api<{ api_key: string }>(
        `/api/custom-providers/${encodeURIComponent(providerId)}/api-key`,
      );
      setRevealedKeys((current) => ({ ...current, [providerId]: result.api_key }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [setBusy, setError]);

  const hideKey = (providerId: string) => setRevealedKeys((current) => {
    const next = { ...current };
    delete next[providerId];
    return next;
  });

  const save = useCallback(async () => {
    setBusy("custom-providers");
    setError(null);
    setSaved(null);
    try {
      const next = await api<CustomProvidersPayload>("/api/custom-providers", {
        method: "POST",
        body: JSON.stringify({
          providers: rows.map((row) => ({
            id: row.id.trim(),
            base_url: row.base_url.trim(),
            model: row.model.trim() || null,
            reasoning_effort: row.reasoning_effort.trim() || null,
            wire_api: row.wire_api,
            pricing: row.pricing.trim() || null,
            require_api_key: row.require_api_key,
            ...(row.api_key === null ? {} : { api_key: row.api_key }),
          })),
        }),
      });
      setPayload(next);
      setDrafts(null);
      setRevealedKeys({});
      setSaved(`已保存 ${next.providers.length} 个端点，重启后生效`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [rows, setBusy, setError]);

  if (!payload) return null;
  const full = rows.length >= payload.max_providers;

  return (
    <div className="settings-section">
      <h4 className="account-subhead">Custom API 端点（{rows.length}/{payload.max_providers}）</h4>
      <datalist id="models-dev-providers">
        {payload.pricing_options.map((option) => <option key={option} value={option} />)}
      </datalist>
      {!rows.length && <div className="empty cards">还没有自定义端点。点「新增端点」接入任意 OpenAI 兼容服务。</div>}
      {rows.map((row, index) => (
        <div className="endpoint-card" key={index}>
          <div className="endpoint-grid">
            <label><span>ID</span>
              <input value={row.id} placeholder="main" disabled={busy !== null}
                onChange={(e) => update(index, { id: e.target.value })} />
            </label>
            <label className="endpoint-wide"><span>Base URL</span>
              <input value={row.base_url} placeholder="https://api.example/v1" disabled={busy !== null}
                onChange={(e) => update(index, { base_url: e.target.value })} />
            </label>
            <label><span>模型</span>
              <input value={row.model} placeholder="gpt-4o" disabled={busy !== null}
                onChange={(e) => update(index, { model: e.target.value })} />
            </label>
            <label><span>API Key</span>
              <input
                type={row.api_key === null && revealedKeys[row.id] !== undefined ? "text" : "password"}
                value={row.api_key ?? revealedKeys[row.id] ?? ""}
                readOnly={row.api_key === null && revealedKeys[row.id] !== undefined}
                placeholder={row.api_key_configured ? `已配置（${row.api_key_masked}）· 留空不变` : "未配置"}
                disabled={busy !== null}
                onChange={(e) => update(index, { api_key: e.target.value })}
              />
            </label>
            <label><span>协议</span>
              <select value={row.wire_api} disabled={busy !== null}
                onChange={(e) => update(index, { wire_api: e.target.value })}>
                {payload.wire_apis.map((w) => <option key={w} value={w}>{w}</option>)}
              </select>
            </label>
            <label data-tooltip="models.dev 的厂商 ID，决定按谁的价折算等效成本。同一模型常被多家转售且价格不同，无法从模型名或地址推断，只能指定。留空则成本显示「—」，且预算自动停止对该端点不生效。">
              <span>计费厂商</span>
              <input
                list="models-dev-providers"
                value={row.pricing}
                placeholder="models.dev 厂商 ID，如 xai · 留空不计成本"
                disabled={busy !== null}
                onChange={(e) => update(index, { pricing: e.target.value })}
              />
            </label>
            <label><span>推理强度</span>
              <select value={row.reasoning_effort} disabled={busy !== null}
                onChange={(e) => update(index, { reasoning_effort: e.target.value })}>
                {["", "low", "medium", "high", "xhigh", "max"].map((o) => (
                  <option key={o} value={o}>{o || "（默认）"}</option>
                ))}
              </select>
            </label>
            <label className="endpoint-check">
              <input type="checkbox" checked={row.require_api_key} disabled={busy !== null}
                onChange={(e) => update(index, { require_api_key: e.target.checked })} />
              <span>需要 API Key</span>
            </label>
            <div className="endpoint-actions">
              {row.api_key_configured && row.api_key === null && (
                revealedKeys[row.id] !== undefined
                  ? <button className="text-button" disabled={busy !== null}
                    onClick={() => hideKey(row.id)}>隐藏密钥</button>
                  : <button className="text-button" disabled={busy !== null}
                    onClick={() => void revealKey(row.id)}>
                    {busy === `reveal-key-${row.id}` ? "读取中…" : "显示密钥"}
                  </button>
              )}
              {row.api_key_configured && row.api_key === null && (
                <button className="text-button" disabled={busy !== null}
                  onClick={() => { hideKey(row.id); update(index, { api_key: "" }); }}>清除密钥</button>
              )}
              {row.api_key !== null && (
                <button className="text-button" disabled={busy !== null}
                  onClick={() => update(index, { api_key: null })}>取消改密钥</button>
              )}
              <button className="text-button danger-text" disabled={busy !== null}
                onClick={() => setDrafts(rows.filter((_, i) => i !== index))}>删除端点</button>
            </div>
          </div>
          {row.extra_header_names.length > 0 && (
            <small className="settings-hint">
              自定义请求头（保留不变）：{row.extra_header_names.join("、")}
            </small>
          )}
        </div>
      ))}
      <div className="settings-actions">
        <button
          className="compact"
          disabled={busy !== null || full}
          title={full ? `最多 ${payload.max_providers} 个` : ""}
          onClick={() => setDrafts([...rows, {
            id: "", base_url: "", model: "", reasoning_effort: "", wire_api: "chat-completions", pricing: "",
            require_api_key: true, extra_header_names: [], api_key_configured: false,
            api_key_masked: "", api_key: "",
          }])}
        >新增端点</button>
        <button className="compact" disabled={busy !== null || !dirty} onClick={save}>
          {busy === "custom-providers" ? "保存中…" : "保存端点"}
        </button>
        <button className="text-button" disabled={busy !== null || !dirty}
          onClick={() => { setDrafts(null); setSaved(null); }}>放弃改动</button>
        {saved && <span className="settings-saved">{saved}</span>}
      </div>
    </div>
  );
}

export function SettingsPanel({
  busy,
  setBusy,
  setError,
}: {
  busy: string | null;
  setBusy: (value: string | null) => void;
  setError: (value: string | null) => void;
}) {
  const [payload, setPayload] = useState<SettingsPayload | null>(null);
  // Only edited keys are tracked, so an untouched secret is never written back
  // as its own mask.
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setPayload(await api<SettingsPayload>("/api/settings"));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [setError]);

  useEffect(() => { load(); }, [load]);

  const dirty = Object.keys(draft).length;

  const save = useCallback(async () => {
    setBusy("settings");
    setError(null);
    setSaved(null);
    try {
      const next = await api<SettingsPayload>("/api/settings", {
        method: "POST",
        body: JSON.stringify({ values: draft }),
      });
      setPayload(next);
      setDraft({});
      setSaved(`已保存 ${dirty} 项到 ${next.path}，重启后生效`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [draft, dirty, setBusy, setError]);

  if (!payload) return <article className="panel settings-panel"><PanelTitle code="09" title="设置" meta="编辑本地 .env" /><div className="empty cards">读取中…</div></article>;

  const shown = (field: SettingsField) =>
    draft[field.key] ?? (field.secret ? "" : field.value ?? "");

  return (
    <article className="panel settings-panel">
      <PanelTitle code="09" title="设置" meta="写入本地 .env · 重启后生效" />
      <p className="settings-note">
        保存只写入 <code>{payload.path}</code>，<strong>不会改变正在运行的进程</strong>；重启后生效。
        密钥只写不读：现有值仅显示掩码尾号，留空表示保持不变。shell 里 export 的同名变量在运行时优先级更高。
      </p>
      <WebUpdatePanel busy={busy} setBusy={setBusy} setError={setError} />
      <BackupPanel busy={busy} setBusy={setBusy} setError={setError} />
      <LogMaintenancePanel busy={busy} setBusy={setBusy} setError={setError} />
      <RestartPanel busy={busy} setBusy={setBusy} setError={setError} />
      <CustomProvidersPanel busy={busy} setBusy={setBusy} setError={setError} />
      {payload.sections.map((section) => (
        <div className="settings-section" key={section.title}>
          <h4 className="account-subhead">{section.title}</h4>
          {section.fields.map((field) => (
            <div className="settings-row" key={field.key}>
              <span className="settings-label">
                <strong>{field.label}</strong>
                <small>{field.key}</small>
              </span>
              <div className="settings-input">
                {field.kind === "enum" ? (
                  <select
                    value={shown(field)}
                    disabled={busy !== null}
                    onChange={(event) => setDraft((c) => ({ ...c, [field.key]: event.target.value }))}
                  >
                    {(field.options.includes("") ? field.options : ["", ...field.options]).map((option) => (
                      <option key={option} value={option}>{option || "（默认）"}</option>
                    ))}
                  </select>
                ) : field.kind === "bool" ? (
                  <select
                    value={shown(field)}
                    disabled={busy !== null}
                    onChange={(event) => setDraft((c) => ({ ...c, [field.key]: event.target.value }))}
                  >
                    <option value="">（默认）</option>
                    <option value="true">true</option>
                    <option value="false">false</option>
                  </select>
                ) : field.kind === "json" ? (
                  <textarea
                    rows={3}
                    placeholder={field.secret && field.configured ? `已配置（${field.masked}）· 留空保持不变` : field.placeholder}
                    value={shown(field)}
                    disabled={busy !== null}
                    onChange={(event) => setDraft((c) => ({ ...c, [field.key]: event.target.value }))}
                  />
                ) : (
                  <input
                    type={field.secret ? "password" : field.kind === "int" || field.kind === "number" ? "number" : "text"}
                    placeholder={field.secret && field.configured ? `已配置（${field.masked}）· 留空保持不变` : field.placeholder}
                    value={shown(field)}
                    disabled={busy !== null}
                    onChange={(event) => setDraft((c) => ({ ...c, [field.key]: event.target.value }))}
                  />
                )}
                {field.description && <small className="settings-hint">{field.description}</small>}
              </div>
              <span className={`settings-state ${field.configured ? "on" : ""}`}>
                {field.secret
                  ? field.configured ? `已配置 ${field.masked}` : "未配置"
                  : field.configured ? "已设置" : "默认"}
              </span>
            </div>
          ))}
        </div>
      ))}
      <div className="settings-actions">
        <button className="compact" disabled={busy !== null || !dirty} onClick={save}>
          {busy === "settings" ? "保存中…" : dirty ? `保存 ${dirty} 项改动` : "无改动"}
        </button>
        <button className="text-button" disabled={busy !== null || !dirty} onClick={() => { setDraft({}); setSaved(null); }}>放弃改动</button>
        {saved && <span className="settings-saved">{saved}</span>}
      </div>
    </article>
  );
}
