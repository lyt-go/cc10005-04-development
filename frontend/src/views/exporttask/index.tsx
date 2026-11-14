import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/exporttask'
const columns = [
  '任务编号',
  '数据源标识',
  '数据源快照编号',
  '接入版本',
  '冻结目标端点',
  '目标系统',
  '投递状态',
  '未发送分片序号',
]
const actions = ['提交任务审批', '启动分发', '恢复分发', '完成分发', '取消分发']
const statuses = ['草稿', '任务审批中', '执行中', '待恢复', '已完成', '已取消']

type Row = Record<string, string | number | null | number[]>
type Manifest = {
  任务编号: string
  分发任务状态: string
  投递状态: string
  数据源快照编号: string
  连接器编号: string
  接入版本: string
  压缩前数据源标识: string
  未发送分片序号: number[]
  暂停原因: string
  snapshot: Row | null
  version: Row | null
  checks: Record<string, boolean | string | number[]>
}

function renderCell(value: Row[string] | undefined) {
  if (Array.isArray(value)) {
    return value.length ? value.join('、') : '—'
  }
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

export default function ExporttaskView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [manifest, setManifest] = useState<Manifest | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 2), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('分发任务列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据分发列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function openManifest(row: Row) {
    setNotice('')
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/manifest`)
      if (!response.ok) {
        throw new Error('投递清单读取失败')
      }
      setManifest(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '投递清单读取失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message || payload.detail || '数据分发动作未生效')
      }
      setNotice(payload.message ?? '')
      await reload()
      if (manifest && manifest.任务编号 === row['任务编号']) {
        await openManifest(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据分发操作失败')
    }
  }

  return (
    <section className="page" data-module="exporttask">
      <header className="page-head">
        <div>
          <h2>数据分发管理</h2>
          <p className="page-desc">
            提交任务时冻结数据源快照，投递清单固定引用当时的连接器端点、协议、编码与接入版本。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('分发任务登记入口尚未接入审批流')}>
            登记分发任务
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据分发清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {[
          { label: '分发任务总数', value: total },
          { label: '执行中任务', value: rows.filter((row) => row['投递状态'] === '执行中').length },
          { label: '待恢复任务', value: rows.filter((row) => row['投递状态'] === '待恢复').length },
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
          <span>投递状态</span>
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
                <button className="link" type="button" onClick={() => void openManifest(row)}>投递清单</button>
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
              <td className="empty-state" colSpan={columns.length + 1}>暂无数据分发数据，可先登记分发任务</td>
            </tr>
          )}
        </tbody>
      </table>

      {manifest && (
        <section className="detail-panel">
          <header className="detail-head">
            <h3>投递清单 · {manifest.任务编号}</h3>
            <button className="btn ghost" type="button" onClick={() => setManifest(null)}>关闭</button>
          </header>
          <dl className="detail-grid">
            <div><dt>分发任务状态</dt><dd>{manifest.分发任务状态}</dd></div>
            <div><dt>数据源快照</dt><dd>{manifest.数据源快照编号 || '—'}</dd></div>
            <div><dt>连接器 / 接入版本</dt><dd>{manifest.连接器编号 || '—'} / {manifest.接入版本 || '—'}</dd></div>
            <div><dt>压缩前数据源标识</dt><dd>{manifest.压缩前数据源标识 || '—'}</dd></div>
            <div><dt>冻结目标端点</dt><dd>{renderCell(manifest.snapshot?.['目标端点'])}</dd></div>
            <div><dt>冻结协议 / 编码</dt><dd>{renderCell(manifest.snapshot?.['协议类型'])} / {renderCell(manifest.snapshot?.['数据编码'])}</dd></div>
            <div><dt>未发送分片序号</dt><dd>{renderCell(manifest.未发送分片序号)}</dd></div>
            <div><dt>暂停原因</dt><dd>{manifest.暂停原因 || '—'}</dd></div>
          </dl>
          <p className={String(manifest.checks['结论']).startsWith('一致') ? 'ok-text' : 'error-text'}>
            {String(manifest.checks['结论'])}
          </p>
          <table className="data-table">
            <thead>
              <tr><th>校验项</th><th>结果</th></tr>
            </thead>
            <tbody>
              {Object.entries(manifest.checks)
                .filter(([, value]) => typeof value === 'boolean')
                .map(([key, value]) => (
                  <tr key={key}><td>{key}</td><td>{value ? '通过' : '不通过'}</td></tr>
                ))}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条数据分发记录</span>
        <span>
          {notice && <span className="ok-text">{notice}　</span>}
          {errorMessage && <span className="error-text">{errorMessage}</span>}
        </span>
      </footer>
    </section>
  )
}
