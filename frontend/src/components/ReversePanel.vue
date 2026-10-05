<template>
  <div class="rev-panel">
    <input v-model="reason" maxlength="200" placeholder="冲正原因（必填）" />
    <div class="rev-actions">
      <button @click="doPreview" :disabled="loading">{{ preview ? '重新预览' : '生成预览' }}</button>
      <button class="btn-warn" :disabled="!canConfirm" @click="doConfirm">确认冲正</button>
    </div>
    <p class="muted">预览模式：全层余量不会变化，确认后才加回。</p>

    <p v-if="error" class="rev-err">{{ errorText }}</p>

    <table v-if="preview && preview.valid" class="rev-table">
      <thead>
        <tr><th>批次</th><th>到期</th><th>冲正前</th><th>补回</th><th>冲正后</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in preview.restorations" :key="r.lot_id">
          <td>#{{ r.lot_id }}</td>
          <td>{{ r.expiry || '—' }}</td>
          <td>{{ statusLabel(r.status_before) }} ×{{ r.qty_before }}</td>
          <td>+{{ r.take }}</td>
          <td>
            <strong :class="{ 'st-expired': r.status_after === 'expired' }">{{ statusLabel(r.status_after) }}</strong>
            ×{{ r.qty_after }}
            <div v-if="r.status_after === 'expired'" class="muted">已下架：余量补回但不回架</div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
<script setup>
import { computed, ref } from 'vue'
import { api } from '../api'

const props = defineProps({ target: { type: Object, required: true } })
const emit = defineEmits(['done'])

const reason = ref('')
const preview = ref(null)
const error = ref('')
const loading = ref(false)

const ERROR_TEXT = {
  not_latest: '该笔已不是最近一笔消费，请刷新履历。',
  already_reversed: '该笔已经冲正过，不能重复冲正。',
  no_consumption: '暂无可冲正的消费记录。',
  lot_missing: '原批次已不存在，无法冲正。',
  inconsistent_qty: '批次余量数据异常，冲正已拒绝。',
}

const errorText = computed(() => ERROR_TEXT[error.value] || error.value)
const canConfirm = computed(
  () => !loading.value && !!preview.value && preview.value.valid && !!reason.value.trim()
)

function statusLabel(s) {
  return { on_shelf: '在架', consumed: '已耗尽', expired: '已下架' }[s] || s
}

async function doPreview() {
  loading.value = true; error.value = ''
  try {
    preview.value = await api('/reverse/preview', {
      method: 'POST', body: JSON.stringify({ consumption_id: props.target.id }),
    })
    if (!preview.value.valid) error.value = preview.value.error
  } catch (e) {
    error.value = e.message; preview.value = null
  } finally { loading.value = false }
}

async function doConfirm() {
  loading.value = true; error.value = ''
  try {
    await api('/reverse', {
      method: 'POST',
      body: JSON.stringify({ consumption_id: props.target.id, reason: reason.value.trim() }),
    })
    window.alert('冲正完成')
    reason.value = ''; preview.value = null
    window.dispatchEvent(new Event('pantry:changed'))
    emit('done')
  } catch (e) {
    // 409 not_latest / already_reversed 等：提示并通知父级刷新
    error.value = e.message
    emit('done')
  } finally { loading.value = false }
}
</script>
