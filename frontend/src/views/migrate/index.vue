<template>
  <section class="page" data-module="migrate">
    <header class="page-head">
      <div>
        <h2>数据迁移管理</h2>
        <p class="page-desc">维护数据迁移的源目标配置、迁移方式与校验规则；大表分段重跑只从下一次续跑位置继续，异常段不会阻断其它已校验段的边界。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记迁移任务</button>
        <button class="btn" type="button" @click="exportRows">导出数据迁移管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>完整边界 / 续跑位置</th>
          <th>异常段</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <template v-if="row.完整边界 !== undefined && row.完整边界 !== null">
              第 {{ row.完整边界 }} 段 · {{ row.下一次续跑位置 || '—' }}
            </template>
            <template v-else>—</template>
          </td>
          <td>
            <span :class="segmentList(row.异常段).length ? 'tag danger' : 'tag'">
              {{ segmentList(row.异常段).length ? `第 ${segmentList(row.异常段).join('、')} 段` : '无' }}
            </span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">核对详情</button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无数据迁移管理数据，可先登记迁移任务</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据迁移管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="detail" class="detail-mask" @click.self="closeDetail">
      <section class="detail-panel">
        <header class="detail-head">
          <div>
            <h3>迁移核对详情 · {{ detail.task.任务编号 }}</h3>
            <p class="page-desc">
              {{ detail.task.源表名 }} → {{ detail.task.目标表名 }} ·
              批次 {{ detail.checklist?.批次号 || '—' }} ·
              校验版本 {{ detail.checklist?.校验版本 ?? '—' }}
            </p>
          </div>
          <div class="page-actions">
            <button class="btn ghost" type="button" @click="reloadDetail">刷新</button>
            <button class="btn ghost" type="button" @click="closeDetail">返回迁移列表</button>
          </div>
        </header>

        <p v-if="detail.error" class="error-text">{{ detail.error }}</p>

        <div class="stat-row">
          <article class="stat-card">
            <span class="stat-label">已迁移范围</span>
            <strong class="stat-value small">{{ detail.checklist?.已迁移范围 || '—' }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">异常段</span>
            <strong class="stat-value small">
              {{ segmentList(detail.checklist?.异常段).length ? segmentList(detail.checklist?.异常段).join('、') : '无' }}
            </strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">下一次续跑位置</span>
            <strong class="stat-value small">{{ detail.checklist?.下一次续跑位置 || '—' }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">迁移完成状态</span>
            <strong class="stat-value small">{{ detail.checklist?.迁移完成状态 || '—' }}</strong>
          </article>
        </div>

        <div class="detail-toolbar">
          <label class="filter-item">
            <span>异常段处理</span>
            <select v-model="strategy" @change="switchStrategy">
              <option value="隔离异常段续跑">隔离异常段续跑</option>
              <option value="停在异常段之前">停在异常段之前</option>
            </select>
          </label>
          <span class="muted">复核清单只保留一个可继续的完整边界：第 {{ detail.checklist?.完整边界 ?? '—' }} 段</span>
        </div>

        <h4>上线前检查</h4>
        <table class="data-table compact">
          <tbody>
            <tr><th>依赖源库</th><td>{{ detail.preflight?.依赖源库 || '—' }}</td><th>依赖目标环境</th><td>{{ detail.preflight?.依赖目标环境 || '—' }}</td></tr>
            <tr><th>回退快照</th><td>{{ detail.preflight?.快照编号 || '—' }}（{{ detail.preflight?.快照存在 ? '已存在' : '缺失' }}）</td><th>结论</th><td>{{ detail.preflight?.结论 || '—' }}</td></tr>
            <tr>
              <th>结构漂移</th>
              <td colspan="3">
                <span v-if="!detail.preflight?.结构漂移?.length">未发现本地配置与线上结构漂移</span>
                <span v-else class="error-text">{{ detail.preflight?.结构漂移?.join('；') }}</span>
              </td>
            </tr>
          </tbody>
        </table>

        <h4>分段校验明细（{{ detail.version?.分段总数 ?? 0 }} 段 / 每段 {{ detail.version?.分段行数 ?? 0 }} 行）</h4>
        <p class="page-desc">
          校验规则：{{ detail.version?.校验规则 || '—' }} ·
          已校验 {{ segmentList(detail.version?.已校验段).length }} 段 ·
          异常 {{ segmentList(detail.version?.异常段).length }} 段 ·
          行数差异 {{ detail.version?.行数差异 ?? 0 }}
        </p>
        <div class="table-scroll">
          <table class="data-table compact">
            <thead>
              <tr>
                <th>段号</th>
                <th>行区间</th>
                <th>目标分区</th>
                <th>源端行数</th>
                <th>目标端行数</th>
                <th>源端唯一数</th>
                <th>目标端唯一数</th>
                <th>源端 amount</th>
                <th>目标端 amount</th>
                <th>校验状态</th>
                <th>异常原因</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="segment in detail.version?.分段明细 ?? []" :key="String(segment.段号)">
                <td>{{ segment.段号 }}</td>
                <td>{{ segment.行区间 || '—' }}</td>
                <td>{{ segment.目标分区 || '—' }}</td>
                <td>{{ segment.源端行数 ?? '—' }}</td>
                <td>{{ segment.目标端行数 ?? '—' }}</td>
                <td>{{ segment['源端 payment_id 唯一数'] ?? '—' }}</td>
                <td>{{ segment['目标端 payment_id 唯一数'] ?? '—' }}</td>
                <td>{{ segment['源端 amount 汇总'] ?? '—' }}</td>
                <td>{{ segment['目标端 amount 汇总'] ?? '—' }}</td>
                <td>
                  <span :class="segment.校验状态 === '异常' ? 'tag danger' : 'tag'">{{ segment.校验状态 }}</span>
                </td>
                <td>{{ segment.异常原因 || '—' }}</td>
              </tr>
              <tr v-if="!(detail.version?.分段明细 ?? []).length">
                <td colspan="11" class="empty-state">该迁移任务没有分段明细，按整表迁移</td>
              </tr>
            </tbody>
          </table>
        </div>

        <h4>迁移核对清单</h4>
        <table class="data-table compact">
          <tbody>
            <tr><th>校验版本</th><td>{{ detail.checklist?.校验版本 ?? '—' }}</td><th>批次号</th><td>{{ detail.checklist?.批次号 || '—' }}</td></tr>
            <tr><th>异常段处理</th><td>{{ detail.checklist?.异常段处理 || '—' }}</td><th>隔离段</th><td>{{ segmentList(detail.checklist?.隔离段).join('、') || '无' }}</td></tr>
            <tr><th>统计报告</th><td>{{ detail.checklist?.统计报告 || '—' }}</td><th>统计版本</th><td>{{ detail.checklist?.统计版本 ?? '—' }}</td></tr>
            <tr><th>统计结论</th><td colspan="3">{{ detail.checklist?.统计结论 || '尚未回传统计结论' }}</td></tr>
          </tbody>
        </table>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, string | number | null | number[]>
type ValidationSegment = Record<string, string | number | null>
type ValidationVersion = {
  校验版本: number
  批次号: string
  校验规则: string
  分段总数: number
  分段行数: number
  已校验段: number[]
  异常段: number[]
  已迁移段: number[]
  隔离段: number[]
  完整边界: number
  已迁移范围: string
  下一次续跑位置: string
  迁移完成状态: string
  行数差异: number
  分段明细: ValidationSegment[]
}
type Checklist = {
  校验版本: number
  批次号: string
  异常段处理: string
  已迁移范围: string
  异常段: number[]
  隔离段: number[]
  完整边界: number
  下一次续跑位置: string
  迁移完成状态: string
  统计报告?: string
  统计版本?: number
  统计结论?: string
}
type Preflight = {
  依赖源库: string
  依赖目标环境: string
  快照编号: string
  快照存在: boolean
  结构漂移: string[]
  结论: string
}
type Detail = {
  task: Row
  version: ValidationVersion | null
  checklist: Checklist | null
  preflight: Preflight | null
  error: string
}

const ENDPOINT = '/api/migrate'
const columns = ["任务编号", "源表名", "目标表名", "迁移方式", "数据范围", "校验规则", "迁移耗时", "任务状态"]
const actions = ["执行迁移", "回滚迁移", "归档任务"]
const filterFields = columns.slice(0, 3)

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const detail = ref<Detail | null>(null)
const strategy = ref('隔离异常段续跑')

const stats = ref([
  { label: '迁移任务总数', value: 0 },
  { label: '迁移中任务', value: 0 },
  { label: '已完成任务', value: 0 },
  { label: '异常分段', value: 0 },
])

function segmentList(value: unknown): number[] {
  return Array.isArray(value) ? (value as number[]) : []
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '迁移任务登记入口尚未接入审批流'
}

function closeDetail() {
  detail.value = null
}

async function loadDetail(row: Row) {
  try {
    const task = await fetchJson<Row>(`${ENDPOINT}/${row.id}`)
    const versionPayload = await fetchJson<{ version: number | null; items: ValidationVersion[] }>(
      `${ENDPOINT}/${row.id}/validations`,
    )
    // 用后端返回的当前版本取明细，避免列表顺序与生效版本不一致。
    const version =
      versionPayload.items?.find((item) => item.校验版本 === versionPayload.version) ??
      versionPayload.items?.[versionPayload.items.length - 1] ??
      null
    let checklist: Checklist | null = null
    let preflight: Preflight | null = null
    let error = ''
    try {
      checklist = await fetchJson<Checklist>(`${ENDPOINT}/${row.id}/checklist`)
    } catch (checklistError) {
      error = checklistError instanceof Error ? checklistError.message : '迁移核对清单读取失败'
    }
    try {
      preflight = await fetchJson<Preflight>(`${ENDPOINT}/${row.id}/preflight`)
    } catch {
      preflight = null
    }
    detail.value = { task, version, checklist, preflight, error }
    if (checklist?.异常段处理) {
      strategy.value = checklist.异常段处理
    }
  } catch (error) {
    detail.value = {
      task: row,
      version: null,
      checklist: null,
      preflight: null,
      error: error instanceof Error ? error.message : '迁移核对详情读取失败',
    }
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  detail.value = null
  await loadDetail(row)
}

async function reloadDetail() {
  if (!detail.value) return
  await loadDetail(detail.value.task)
}

async function switchStrategy() {
  if (!detail.value) return
  const row = detail.value.task
  try {
    const response = await request(`${ENDPOINT}/${row.id}/validation-strategy`, {
      method: 'POST',
      body: JSON.stringify({ 异常段处理: strategy.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.detail || payload.message || '异常段处理切换失败')
    }
    await loadDetail(row)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '异常段处理切换失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      // 归档被异常段拦下时把后端原因原样显示，避免看起来像操作成功。
      throw new Error(payload.detail || payload.message || '数据迁移管理动作未生效，请稍后重试')
    }
    await reload()
    if (detail.value && String(detail.value.task.id) === String(row.id)) {
      await loadDetail(row)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据迁移管理操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('迁移任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '迁移任务总数', value: total.value },
      { label: '迁移中任务', value: rows.value.filter((row) => row['任务状态'] === '迁移中' || row.status === '迁移中').length },
      { label: '已完成任务', value: rows.value.filter((row) => row['任务状态'] === '已完成' || row.status === '已完成').length },
      { label: '异常分段', value: rows.value.reduce((sum, row) => sum + segmentList(row.异常段).length, 0) },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据迁移管理列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.stat-value.small { font-size: 14px; line-height: 1.4; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 10px; background: #eef2f7; font-size: 12px; }
.tag.danger { background: #fde8e8; color: #b42318; }
.detail-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  justify-content: flex-end;
  z-index: 20;
}
.detail-panel {
  width: min(1080px, 94vw);
  height: 100%;
  overflow-y: auto;
  background: #fff;
  padding: 16px 20px 32px;
}
.detail-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.detail-head h3 { margin: 0; }
.detail-toolbar { display: flex; gap: 16px; align-items: flex-end; margin: 12px 0; }
.muted { color: var(--muted); font-size: 12px; }
.table-scroll { max-height: 340px; overflow: auto; }
.data-table.compact th, .data-table.compact td { padding: 6px 8px; font-size: 12px; }
h4 { margin: 18px 0 8px; font-size: 14px; }
</style>
