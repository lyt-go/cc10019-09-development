<template>
  <section class="page" data-module="behavior_path">
    <header class="page-head">
      <div><h2>行为路径</h2><p class="page-desc">维护用户行为路径的Sankey模型定义与会话切割规则，为行为路径提供可验证的数据工程流程。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记路径模型</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出关系清单</button>
      </div>
    </header>
    <div class="dag-wrap" ref="dagRef">
      <svg class="dag-svg" :viewBox="`0 0 ${svgW} ${svgH}`">
        <defs><marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6" fill="#94a3b8"/></marker></defs>
        <g v-for="edge in graph.edges" :key="edge.from+edge.to">
          <line class="dag-edge" :x1="nodePos[edge.from]?.x||0" :y1="nodePos[edge.from]?.y||0" :x2="nodePos[edge.to]?.x||0" :y2="nodePos[edge.to]?.y||0"/>
          <text :x="(nodePos[edge.from]?.x+nodePos[edge.to]?.x)/2" :y="(nodePos[edge.from]?.y+nodePos[edge.to]?.y)/2 - 6" font-size="10" fill="#64748b" text-anchor="middle">{{ edge.label }}</text>
        </g>
        <g v-for="node in graph.nodes" :key="node.id" :class="['dag-node', {selected: selectedNode?.id===node.id}]" @click="selectNode(node)" @mousedown="startDrag($event, node)">
          <rect :x="nodePos[node.id]?.x - 60" :y="nodePos[node.id]?.y - 20" width="120" height="40" rx="6" :fill="nodeColor(node.status)"/>
          <text :x="nodePos[node.id]?.x" :y="nodePos[node.id]?.y + 4" text-anchor="middle">{{ node.name || node.label }}</text>
        </g>
      </svg>
    </div>
    <div v-if="selectedNode" class="dag-detail" data-testid="dag-detail">
      <dl><dt>编号</dt><dd>{{ selectedNode.id }}</dd></dl>
      <dl><dt>名称</dt><dd>{{ selectedNode.name || selectedNode.label }}</dd></dl>
      <dl v-for="f in detailFields" :key="f"><dt>{{ f }}</dt><dd>{{ selectedNode[f] ?? '—' }}</dd></dl>
      <div style="grid-column:1/-1; margin-top:8px; display:flex; gap:6px;">
        <button v-for="action in actions" :key="action" class="btn" :data-testid="`action-${selectedNode.id}-${action}`" @click="runAction(action, selectedNode)">{{ action }}</button>
      </div>
    </div>
    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记路径模型</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
    <section class="detail-panel" data-testid="report-versions">
      <header class="detail-head">
        <div>
          <h3>路径报告版本对比</h3>
          <p>补报并进会话明细后，旧报告保持原版本；只有重新分析才产出新版本，可逐条核对哪些连边实际受影响。</p>
        </div>
        <div class="page-actions">
          <label class="inline-field">
            <span>路径模型</span>
            <select v-model="reportModel" data-testid="report-model" @change="loadVersions">
              <option v-for="item in modelOptions" :key="item.model_id" :value="item.model_id">{{ item.model_id }}</option>
            </select>
          </label>
          <button class="btn" data-testid="report-reload" type="button" @click="loadVersions">刷新版本</button>
          <button class="btn primary" data-testid="report-rebuild" type="button" @click="rebuildReport">重新分析</button>
        </div>
      </header>
      <p v-if="!versions" class="empty-state">选择路径模型后查看报告版本与补报归属</p>
      <template v-else>
        <div class="detail-meta">
          <span>报告版本 <strong>v{{ versions.version }}</strong></span>
          <span>上一版本 <strong>{{ versions.previous_version ? `v${versions.previous_version}` : '—' }}</strong></span>
          <span>会话明细版本 <strong>v{{ versions.session_detail_version }}</strong></span>
          <span>触发方式 <strong>{{ versions.trigger }}</strong></span>
          <span>生成时间 <strong>{{ versions.generated_at || '—' }}</strong></span>
        </div>
        <div class="detail-meta">
          <span>受影响连边 <strong>{{ versions.edge_summary.shortened + versions.edge_summary.added + (versions.edge_summary.changed || 0) }}</strong></span>
          <span>保持不变 <strong>{{ versions.edge_summary.unchanged }}</strong></span>
          <span>已消失 <strong>{{ versions.edge_summary.removed }}</strong></span>
        </div>
        <table class="data-table">
          <thead><tr><th>连边</th><th>本版数量</th><th>上一版数量</th><th>变化</th><th>相关补报</th></tr></thead>
          <tbody>
            <tr v-for="edge in changedEdges" :key="edge.edge_key" :data-testid="`edge-${edge.edge_key}`">
              <td>{{ edge.edge_key }}</td>
              <td>{{ edge.value }}</td>
              <td>{{ edge.previous_value ?? '—' }}</td>
              <td>{{ EDGE_LABELS[edge.change] || edge.change }}</td>
              <td>{{ edge.backfill_uids.join('、') || '—' }}</td>
            </tr>
            <tr v-if="!changedEdges.length"><td colspan="5" class="empty-state">本版所有连边与上一版一致</td></tr>
          </tbody>
        </table>
        <table class="data-table" style="margin-top:10px">
          <thead><tr><th>补报事件</th><th>状态</th><th>是否并入本版</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="item in versions.backfills" :key="item.event_uid" :data-testid="`report-backfill-${item.event_uid}`">
              <td>{{ item.event_uid }}</td>
              <td>{{ item.status }}</td>
              <td>{{ item.included ? '已并入' : '与本版本分开' }}</td>
              <td>{{ item.reason }}</td>
            </tr>
            <tr v-if="!versions.backfills.length"><td colspan="4" class="empty-state">暂无补报事件</td></tr>
          </tbody>
        </table>
      </template>
    </section>
    <footer class="page-foot"><span>{{ graph.nodes.length }} 个节点</span></footer>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/behavior_path'
// 报告版本接口按后端注册的连字符路径访问。
const REPORT_ENDPOINT = '/api/behavior-path'
const columns = ["模型编号", "起始事件", "目标事件", "时间窗口", "路径深度", "会话标识", "路径过滤", "分析区间"]
const actions = ["提交校验", "标记生效", "停用模型"]
const statuses = ["草稿", "待校验", "已生效", "已停用"]
const STATUS_COLORS: Record<string,string> = { '草稿':'#e2e8f0','待校验':'#fef3c7','已启用':'#d1fae5','已停用':'#fee2e2' }
const nodeColor = (s: string) => STATUS_COLORS[s] || '#e2e8f0'
const graph = reactive<{nodes: any[], edges: any[]}>({ nodes: [], edges: [] })
const nodePos = ref<Record<number,{x:number;y:number}>>({})
const selectedNode = ref<any>(null)
const detailFields = computed(() => columns.filter((f: string) => !['id','name','label','status'].includes(f)))
const svgW = ref(800); const svgH = ref(400)
const errorMessage = ref('')
const creating = ref(false); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const reportModel = ref('')
const modelOptions = ref<{ model_id: string }[]>([])
const versions = ref<any>(null)
const EDGE_LABELS: Record<string,string> = { added: '新增', shortened: '数量变化', changed: '数量变化', unchanged: '不变', removed: '已消失' }
const changedEdges = computed(() => (versions.value?.affected_edges || []).filter((edge: any) => edge.change !== 'unchanged'))
let dragNode: any = null; const dagRef = ref<HTMLElement|null>(null)
function layout(nodes: any[]) {
  const w = 800; const h = Math.max(400, nodes.length * 80)
  svgW.value = w; svgH.value = h
  const pos: Record<number,{x:number;y:number}> = {}
  nodes.forEach((n,i) => { pos[n.id] = { x: 120 + (i % 5) * 150, y: 50 + Math.floor(i / 5) * 80 } })
  nodePos.value = pos
}
function selectNode(node: any) { selectedNode.value = selectedNode.value?.id === node.id ? null : node }
function startDrag(e: MouseEvent, node: any) { dragNode = node; const onMove = (ev: MouseEvent) => { if(!dragNode) return; nodePos.value[node.id] = { x: ev.offsetX, y: ev.offsetY } }; const onUp = () => { dragNode = null; document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp) }; document.addEventListener('mousemove', onMove); document.addEventListener('mouseup', onUp) }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); graph.nodes = data.nodes || []; graph.edges = data.edges || []; layout(graph.nodes) } catch(e) { errorMessage.value = '加载失败' }
  await loadModels()
}

async function loadModels() {
  try {
    const res = await request(REPORT_ENDPOINT)
    if (!res.ok) return
    const data = await res.json()
    modelOptions.value = data.items ?? []
    if (!reportModel.value && modelOptions.value.length) {
      reportModel.value = modelOptions.value[0].model_id
    }
    if (reportModel.value) await loadVersions()
  } catch {
    modelOptions.value = []
  }
}
async function loadVersions() {
  if (!reportModel.value) return
  try {
    const res = await request(`${REPORT_ENDPOINT}/${reportModel.value}/versions`)
    versions.value = res.ok ? await res.json() : null
    if (!res.ok) errorMessage.value = '报告版本读取失败'
  } catch(e) { versions.value = null; errorMessage.value = '报告版本读取失败' }
}
async function rebuildReport() {
  if (!reportModel.value) return
  errorMessage.value = ''
  try {
    const res = await request(`${REPORT_ENDPOINT}/${reportModel.value}/build`, { method: 'POST' })
    if (!res.ok) throw new Error('重新分析失败')
    await loadVersions()
  } catch(e: any) { errorMessage.value = e.message || '重新分析失败' }
}
async function runAction(action: string, node: any) {
  try { await request(`${ENDPOINT}/${node.id}/actions`, { method: 'POST', body: JSON.stringify({ action }) }); await reload() } catch(e) { errorMessage.value = '操作失败' }
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
onMounted(reload)
</script>
