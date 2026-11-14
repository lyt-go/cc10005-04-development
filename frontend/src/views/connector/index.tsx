import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/connector'
const columns = [
  '连接器编号',
  '协议类型',
  '目标端点',
  '数据编码',
  '接入版本',
  '版本状态',
  '所属单位',
  'status',
]
const actions = ['发起连通测试', '标记就绪', '标记离线', '改版', '确认版本']
const statuses = ['草稿', '连通测试中', '已就绪', '已离线']

type Row = Record<string, string | number | null>
type Version = Record<string, string | number | null>

function renderCell(value: Row[string]) {
  if (Array.isArray(value)) {
    return value.length ? value.join('、') : '—'
  }
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

export default function ConnectorView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [versions, setVersions] = useState<Version[] | null>(null)
  const [versionOwner, setVersionOwner] = useState('')
  const filterFields = useMemo(() => ['连接器编号', '目标端点'], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('数据连接器列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '连接器管理列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function openVersions(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/versions`)
      if (!response.ok) {
        throw new Error('接入版本读取失败')
      }
      const payload = await response.json()
      setVersions(payload.items ?? [])
      setVersionOwner(String(row['连接器编号'] ?? ''))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '接入版本读取失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      let body: Record<string, string> = { action }
      if (action === '改版') {
        const endpoint = window.prompt('新的目标端点（连接器改版只会生成待确认版本）', String(row['目标端点'] ?? ''))
        if (!endpoint) return
        body = { action, 目标端点: endpoint }
      }
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify(body),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message || payload.detail || '连接器管理动作未生效')
      }
      setNotice(payload.message ?? '')
      await reload()
      if (versions && versionOwner === String(row['连接器编号'] ?? '')) {
        await openVersions(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '连接器管理操作失败')
    }
  }

  return (
    <section className="page" data-module="connector">
      <header className="page-head">
        <div>
          <h2>连接器管理</h2>
          <p className="page-desc">
            管理各类数据源的连接器配置。改版只生成新的待确认接入版本，不会改写已冻结的分发任务快照。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('数据连接器登记入口尚未接入审批流')}>
            登记数据连接器
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出连接器管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {[
          { label: '连接器总数', value: total },
          { label: '已就绪连接器', value: rows.filter((row) => row.status === '已就绪').length },
          { label: '待确认版本', value: rows.filter((row) => row['版本状态'] === '待确认').length },
        ].map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>状态</span>
          <select
            value={filters.status ?? ''}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{renderCell(row[column])}</td>)}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void openVersions(row)}>接入版本</button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无连接器管理数据，可先登记数据连接器</td>
            </tr>
          )}
        </tbody>
      </table>

      {versions && (
        <section className="detail-panel">
          <header className="detail-head">
            <h3>接入版本 · {versionOwner}</h3>
            <button className="btn ghost" type="button" onClick={() => setVersions(null)}>关闭</button>
          </header>
          <table className="data-table">
            <thead>
              <tr>
                <th>接入版本</th><th>版本状态</th><th>协议类型</th><th>目标端点</th>
                <th>数据编码</th><th>占用任务号</th><th>创建时间</th><th>确认时间</th>
              </tr>
            </thead>
            <tbody>
              {versions.map((version) => (
                <tr key={String(version.id)}>
                  <td>{renderCell(version['接入版本'])}</td>
                  <td>{renderCell(version.status)}</td>
                  <td>{renderCell(version['协议类型'])}</td>
                  <td>{renderCell(version['目标端点'])}</td>
                  <td>{renderCell(version['数据编码'])}</td>
                  <td>{renderCell(version['占用任务号'] as unknown as Row[string])}</td>
                  <td>{renderCell(version['创建时间'])}</td>
                  <td>{renderCell(version['确认时间'])}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条连接器管理记录</span>
        <span>
          {notice && <span className="ok-text">{notice}　</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}
