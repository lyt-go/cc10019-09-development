<template>
  <section class="page" data-module="session_split">
    <header class="page-head">
      <div>
        <h2>会话切割管理</h2>
        <p class="page-desc">维护用户会话的切割规则、超时判定与跨渠道归因策略，为会话切割提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记切割策略</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出会话切割清单</button>
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
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无会话切割数据，可先登记切割策略</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记切割策略</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section class="detail-panel" data-testid="session-detail">
      <header class="detail-head">
        <div>
          <h3>会话明细回查</h3>
          <p>以一次确认的会话明细为准，逐条核对补报事件落在哪个会话、有没有进入当前路径报告。</p>
        </div>
        <div class="page-actions">
          <label class="inline-field">
            <span>切割策略</span>
            <select v-model="detailStrategy" data-testid="detail-strategy" @change="loadDetail">
              <option v-for="row in strategyOptions" :key="row.strategy_id" :value="row.strategy_id">{{ row.strategy_id }}</option>
            </select>
          </label>
          <button class="btn" data-testid="detail-reload" type="button" @click="loadDetail">刷新明细</button>
        </div>
      </header>
      <div v-if="detail" class="detail-meta">
        <span>明细版本 <strong>v{{ detail.version }}</strong></span>
        <span>确认状态 <strong>{{ detail.confirmed ? '已确认' : '未确认' }}</strong></span>
        <span>会话数 <strong>{{ detail.session_count }}</strong></span>
        <span>事件数 <strong>{{ detail.total_events }}</strong></span>
        <span>超时阈值 <strong>{{ detail.timeout_sec }}s</strong></span>
      </div>
      <p v-else class="empty-state">选择一条切割策略查看已确认的会话明细</p>
      <table v-if="detail" class="data-table">
        <thead>
          <tr><th>补报事件号</th><th>用户</th><th>事件</th><th>状态</th><th>归属会话</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in detail.backfills" :key="item.event_uid" :data-testid="`backfill-${item.event_uid}`">
            <td>{{ item.event_uid }}</td>
            <td>{{ item.user_id ?? '—' }}</td>
            <td>{{ item.event_id ?? '—' }}</td>
            <td>{{ item.status ?? '—' }}</td>
            <td>{{ item.session_ids.join('、') || '—' }}</td>
            <td>{{ item.reason }}</td>
          </tr>
          <tr v-if="!detail.backfills.length">
            <td colspan="6" class="empty-state">暂无补报事件</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条会话切割记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

type BackfillAttribution = {
  event_uid: string
  user_id: string | null
  event_id: string | null
  status: string | null
  session_ids: string[]
  reason: string
  included: boolean
}

type SessionDetail = {
  strategy_id: string
  version: number
  confirmed: boolean
  timeout_sec: number
  session_count: number
  total_events: number
  backfills: BackfillAttribution[]
}

const ENDPOINT = '/api/session_split'
// 明细接口按后端注册的连字符路径访问，避免与列表页的历史路径混用。
const DETAIL_ENDPOINT = '/api/session-split'
const columns = ["策略编号", "策略名称", "超时阈值", "跨天策略", "跨渠道策略", "归因窗口", "会话标识", "策略版本"]
const actions = ["提交校验", "标记生效", "停用策略"]
const statuses = ["草稿", "待校验", "已生效", "已停用"]
const metricNames = ["切割策略总数", "待校验策略", "已生效策略"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const detailStrategy = ref('')
const detail = ref<SessionDetail | null>(null)
const strategyOptions = ref<{ strategy_id: string }[]>([])
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'session_split.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('会话切割动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '会话切割操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('切割策略列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '会话切割列表读取失败'
  }
  await loadStrategies()
}

async function loadStrategies() {
  try {
    const response = await request(DETAIL_ENDPOINT)
    if (!response.ok) return
    const payload = await response.json()
    strategyOptions.value = payload.items ?? []
    if (!detailStrategy.value && strategyOptions.value.length) {
      detailStrategy.value = strategyOptions.value[0].strategy_id
    }
    if (detailStrategy.value) await loadDetail()
  } catch {
    strategyOptions.value = []
  }
}

async function loadDetail() {
  if (!detailStrategy.value) return
  try {
    const response = await request(`${DETAIL_ENDPOINT}/${detailStrategy.value}/detail`)
    if (!response.ok) throw new Error('会话明细读取失败')
    detail.value = await response.json()
  } catch (error) {
    detail.value = null
    errorMessage.value = error instanceof Error ? error.message : '会话明细读取失败'
  }
}

onMounted(reload)
</script>
