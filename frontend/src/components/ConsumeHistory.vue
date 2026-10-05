<template>
  <div>
    <h2>消费履历</h2>
    <p class="muted">只有最近一笔未冲正的成功扣减可以冲正；冲正必须填写原因。</p>
    <div v-if="!history.length" class="muted">暂无消费记录。</div>
    <div v-for="r in history" :key="r.kind + r.id" class="hist-row" :class="{ reversed: r.reversed, reversal: r.kind === 'reverse' }">
      <template v-if="r.kind === 'consume'">
        <div>
          <strong>#{{ r.id }} 消费</strong>
          {{ r.item_name }} ×{{ totalTake(r) }}
          <span class="muted">{{ fmt(r.created_at) }} {{ r.note ? '· ' + r.note : '' }}</span>
          <span v-if="r.reversed" class="badge badge-rev">已冲正 · {{ r.reason }}</span>
        </div>
        <button v-if="r.id === latestOpenId" class="btn-mini" @click="startReverse(r)">冲正</button>
        <span v-else-if="!r.reversed" class="muted">非最近一笔，不可冲</span>
      </template>
      <template v-else>
        <div>
          <strong>#{{ r.id }} 冲正</strong>
          <span class="muted">加回 #{{ r.source_id }} · {{ fmt(r.created_at) }} · 原因：{{ r.reason }}</span>
        </div>
      </template>

      <div v-if="reversingId === r.id" class="reverse-panel">
        <input v-model="reason" placeholder="冲正原因（必填）" />
        <div class="row">
          <button class="btn-mini" :disabled="!reason.trim()" @click="preview">预览</button>
          <button class="btn-mini btn-confirm" :disabled="!reason.trim() || !previewData || previewData.consumption_id !== r.id" @click="confirm">确认冲正</button>
          <button class="btn-mini btn-cancel" @click="cancel">取消</button>
        </div>
        <p v-if="previewData && previewData.consumption_id === r.id" class="muted">
          预览不会改动余量；确认后余量与状态才落库。
        </p>
        <table v-if="previewData && previewData.consumption_id === r.id" class="pv">
          <thead><tr><th>批次/到期</th><th>加回</th><th>余量</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="x in previewData.restores" :key="x.lot_id">
              <td>#{{ x.lot_id }} · {{ x.expiry }}</td>
              <td>+{{ x.add_back }}</td>
              <td>{{ x.before_qty_remain }} → {{ x.after_qty_remain }}</td>
              <td>{{ statusLabel(x.before_status) }} → {{ statusLabel(x.after_status) }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="previewData && previewData.consumption_id === r.id" class="totals">
          <span class="muted">全层余量合计（预览不变，确认后生效）：</span>
          <span v-for="(t, L) in previewData.totals" :key="L" class="lot">
            {{ layerLabel[L] || L }} {{ t.before }} → <b :class="{ changed: t.after !== t.before }">{{ t.after }}</b>
          </span>
        </div>
        <p v-if="error" class="err">{{ error }}</p>
      </div>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const history = ref([])
const latestOpenId = ref(null)
const reversingId = ref(null)
const reason = ref('')
const previewData = ref(null)
const error = ref('')

const layerLabel = { upper: '上层', mid: '中层', lower: '下层' }
const statusMap = { on_shelf: '在架', consumed: '已扣尽', expired: '已下架·过期' }
const statusLabel = s => statusMap[s] || s

function totalTake(r) {
  return (r.result?.deductions || []).reduce((s, d) => s + (d.take || 0), 0)
}
function fmt(ts) { return (ts || '').replace('T', ' ').slice(0, 16) }
function notifyChanged() { window.dispatchEvent(new CustomEvent('pantry-changed')) }

async function loadHistory() {
  history.value = await api('/consumptions')
  // Endpoint is id DESC: first open consume row is the latest open consumption.
  const open = history.value.find(r => r.kind === 'consume' && !r.reversed)
  latestOpenId.value = open ? open.id : null
}

function startReverse(r) {
  reversingId.value = r.id
  reason.value = ''
  previewData.value = null
  error.value = ''
}
function cancel() {
  reversingId.value = null
  reason.value = ''
  previewData.value = null
  error.value = ''
}
async function preview() {
  error.value = ''
  previewData.value = null
  try {
    previewData.value = await api(`/consumptions/${reversingId.value}/reverse/preview`, {
      method: 'POST', body: JSON.stringify({ reason: reason.value }),
    })
  } catch (e) { error.value = e.message }
}
async function confirm() {
  error.value = ''
  try {
    await api(`/consumptions/${reversingId.value}/reverse`, {
      method: 'POST', body: JSON.stringify({ reason: reason.value }),
    })
    reversingId.value = null
    reason.value = ''
    previewData.value = null
    await Promise.all([loadHistory(), notifyChanged()])
  } catch (e) {
    error.value = e.message
    // 409 not_latest / already_reversed: refresh so the button moves/disappears.
    await loadHistory()
    previewData.value = null
  }
}

defineExpose({ reload: loadHistory })
onMounted(loadHistory)
</script>
<style scoped>
.hist-row { background: #fff; border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; margin: 8px 0; }
.hist-row.reversed { opacity: .7; }
.hist-row.reversal { background: #f0f7f2; }
.badge { border-radius: 999px; padding: 2px 10px; font-size: 12px; margin-left: 6px; }
.badge-rev { background: #e7d9f2; color: #6b3c8f; }
.btn-mini { padding: 5px 12px; margin: 6px 6px 0 0; width: auto; }
.btn-confirm { background: #2e7d4f; }
.btn-cancel { background: #6b7f82; }
.row { display: flex; flex-wrap: wrap; align-items: center; }
.reverse-panel { margin-top: 10px; border-top: 1px dashed var(--line); padding-top: 10px; }
.pv { border-collapse: collapse; font-size: 13px; margin: 6px 0; }
.pv th, .pv td { border: 1px solid var(--line); padding: 4px 10px; }
.totals { margin-top: 6px; }
.totals .lot { margin: 4px 6px 0 0; }
.changed { color: var(--teal); }
.err { color: var(--alert); }
</style>
