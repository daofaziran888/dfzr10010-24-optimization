<template>
  <section class="page" data-module="stats">
    <header class="page-head">
      <div>
        <h2>表统计信息管理</h2>
        <p class="page-desc">维护表统计的行数、文件数与容量分析；按批次消费数据迁移管理的同一份源目标一致性校验结果，并把结论回写到迁移核对清单。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记统计报告</button>
        <button class="btn" type="button" @click="exportRows">导出表统计信息清单</button>
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
          <th>迁移批次 / 校验版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <template v-if="row.统计批次 || row.校验版本">
              {{ row.统计批次 || '—' }} · v{{ row.校验版本 ?? '—' }}
            </template>
            <template v-else>—</template>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openConsume(row)">读取校验结果</button>
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
          <td :colspan="columns.length + 2" class="empty-state">暂无表统计信息数据，可先登记统计报告</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条表统计信息记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="consume" class="detail-mask" @click.self="closeConsume">
      <section class="detail-panel">
        <header class="detail-head">
          <div>
            <h3>迁移校验结果消费 · {{ consume.report.报告编号 }}</h3>
            <p class="page-desc">与数据迁移管理读取同一份源目标一致性校验结果，按批次记录行数与异常段。</p>
          </div>
          <div class="page-actions">
            <button class="btn ghost" type="button" @click="reloadConsume">刷新</button>
            <button class="btn ghost" type="button" @click="closeConsume">返回统计列表</button>
          </div>
        </header>

        <p v-if="consume.error" class="error-text">{{ consume.error }}</p>

        <div class="filter-bar">
          <label class="filter-item">
            <span>迁移任务编号</span>
            <input v-model="consume.taskRef" placeholder="例如 MIGR-0004" />
          </label>
          <button class="btn" type="button" @click="reloadConsume">读取当前版本</button>
          <button class="btn primary" type="button" @click="collectBatch">按批次采集并回写清单</button>
        </div>

        <div v-if="consume.version" class="stat-row">
          <article class="stat-card">
            <span class="stat-label">校验版本 / 批次</span>
            <strong class="stat-value small">v{{ consume.version.校验版本 }} · {{ consume.version.批次号 }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">行数（源 / 目标 / 差异）</span>
            <strong class="stat-value small">{{ consume.version.源端行数 }} / {{ consume.version.目标端行数 }} / {{ consume.version.行数差异 }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">异常段</span>
            <strong class="stat-value small">{{ segmentList(consume.version.异常段).join('、') || '无' }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">下一次续跑位置</span>
            <strong class="stat-value small">{{ consume.version.下一次续跑位置 }}</strong>
          </article>
        </div>

        <h4>按批次记录</h4>
        <table class="data-table compact">
          <thead>
            <tr>
              <th>批次号</th>
              <th>校验版本</th>
              <th>已校验段</th>
              <th>异常段</th>
              <th>行数（源/目标/差异）</th>
              <th>完整边界</th>
              <th>下一续跑位置</th>
              <th>回写结论</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="batch in consume.batches" :key="batch.批次号">
              <td>{{ batch.批次号 }}</td>
              <td>v{{ batch.校验版本 }}</td>
              <td>{{ segmentList(batch.已校验段).join('、') || '—' }}</td>
              <td>{{ segmentList(batch.异常段).join('、') || '无' }}</td>
              <td>{{ batch.行数?.源端 }} / {{ batch.行数?.目标端 }} / {{ batch.行数?.差异 }}</td>
              <td>{{ batch.完整边界 }}</td>
              <td>{{ batch.下一次续跑位置 }}</td>
              <td>{{ batch.回写结论 || '—' }}</td>
            </tr>
            <tr v-if="!consume.batches.length">
              <td colspan="8" class="empty-state">还没有按批次采集的统计记录</td>
            </tr>
          </tbody>
        </table>

        <h4>分段明细（只读，来自迁移校验结果）</h4>
        <div class="table-scroll">
          <table class="data-table compact">
            <thead>
              <tr>
                <th>段号</th>
                <th>行区间</th>
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
              <tr v-for="segment in consume.version?.分段明细 ?? []" :key="String(segment.段号)">
                <td>{{ segment.段号 }}</td>
                <td>{{ segment.行区间 || '—' }}</td>
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
              <tr v-if="!(consume.version?.分段明细 ?? []).length">
                <td colspan="10" class="empty-state">当前版本没有分段明细</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, string | number | null>
type Segment = Record<string, string | number | null>
type ValidationVersion = {
  校验版本: number
  批次号: string
  源端行数: number
  目标端行数: number
  行数差异: number
  异常段: number[]
  已校验段: number[]
  下一次续跑位置: string
  分段明细: Segment[]
}
type Batch = {
  批次号: string
  校验版本: number
  已校验段: number[]
  异常段: number[]
  行数: { 源端: number; 目标端: number; 差异: number }
  完整边界: number
  下一次续跑位置: string
  回写结论?: string
}
type Consume = {
  report: Row
  taskRef: string
  version: ValidationVersion | null
  batches: Batch[]
  error: string
}

const ENDPOINT = '/api/stats'
const columns = ["报告编号", "表名称", "行总数", "文件总数", "总容量", "列统计", "统计时间", "报告状态"]
const actions = ["采集统计", "标记过期", "清理报告"]
const filterFields = columns.slice(0, 3)

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const consume = ref<Consume | null>(null)
const stats = ref([
  { label: '统计报告总数', value: 0 },
  { label: '已采集报告', value: 0 },
  { label: '已过期报告', value: 0 },
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
  errorMessage.value = '统计报告登记入口尚未接入审批流'
}

function closeConsume() {
  consume.value = null
}

async function loadConsume(report: Row, taskRef: string) {
  const query = new URLSearchParams({ task: taskRef }).toString()
  let version: ValidationVersion | null = null
  let error = ''
  try {
    version = await fetchJson<ValidationVersion>(`${ENDPOINT}/${report.id}/validation?${query}`)
  } catch (validationError) {
    error = validationError instanceof Error ? validationError.message : '源目标一致性校验结果读取失败'
  }
  let batches: Batch[] = []
  try {
    const payload = await fetchJson<{ items: Batch[] }>(`${ENDPOINT}/${report.id}/batches?${query}`)
    batches = payload.items ?? []
  } catch {
    batches = []
  }
  consume.value = { report, taskRef, version, batches, error }
}

async function openConsume(row: Row) {
  errorMessage.value = ''
  const taskRef = String(row.迁移任务编号 || '')
  await loadConsume(row, taskRef)
}

async function reloadConsume() {
  if (!consume.value) return
  await loadConsume(consume.value.report, consume.value.taskRef)
}

async function collectBatch() {
  if (!consume.value) return
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${consume.value.report.id}/collect`, {
      method: 'POST',
      body: JSON.stringify({ 任务编号: consume.value.taskRef }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '统计报告按批次采集失败')
    }
    await reloadConsume()
    await reload()
  } catch (error) {
    consume.value.error = error instanceof Error ? error.message : '统计报告按批次采集失败'
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
      throw new Error(payload.detail || payload.message || '表统计信息动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '表统计信息操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('统计报告列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value = [
      { label: '统计报告总数', value: total.value },
      { label: '已采集报告', value: rows.value.filter((row) => row.status === '已采集').length },
      { label: '已过期报告', value: rows.value.filter((row) => row.status === '已过期').length },
      { label: '异常分段', value: rows.value.reduce((sum, row) => sum + segmentList(row.异常段).length, 0) },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '表统计信息列表读取失败'
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
.table-scroll { max-height: 320px; overflow: auto; }
.data-table.compact th, .data-table.compact td { padding: 6px 8px; font-size: 12px; }
h4 { margin: 18px 0 8px; font-size: 14px; }
</style>
