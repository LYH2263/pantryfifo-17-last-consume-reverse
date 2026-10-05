<template>
  <div>
    <h1>按临期消费</h1>
    <select v-model.number="item_id"><option v-for="i in items" :value="i.id">{{ i.name }}</option></select>
    <input type="number" v-model.number="qty" />
    <button @click="go">FEFO 扣减</button>
    <pre>{{ result }}</pre>

    <div v-if="latest" class="latest-card">
      <h2>最近一笔消费 #{{ latest.id }}</h2>
      <div>
        <strong>{{ latest.item_name }}</strong>
        <span v-for="(d, i) in latest.deductions" :key="i" class="lot">
          批#{{ d.lot_id }} ×{{ d.take }} · {{ d.expiry || '无到期' }}
        </span>
      </div>
      <p v-if="latest.reversed_at" class="muted">该笔已冲正，原因：{{ latest.reverse_reason }}</p>
      <ReversePanel v-else-if="latest.reversible" :target="latest" @done="loadLatest" />
      <p v-else class="muted">仅可冲正最近一笔成功消费，请前往「履历」查看。</p>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import ReversePanel from '../components/ReversePanel.vue'
const items = ref([])
const item_id = ref(1)
const qty = ref(1)
const result = ref('')
const latest = ref(null)
onMounted(async () => {
  items.value = await api('/items')
  if (items.value[0]) item_id.value = items.value[0].id
  await loadLatest()
})
async function loadLatest() {
  const rows = await api('/consumptions')
  latest.value = rows.find(r => r.kind === 'consume') || null
}
async function go() {
  try {
    result.value = JSON.stringify(await api('/consume', { method: 'POST', body: JSON.stringify({ item_id: item_id.value, qty: qty.value }) }), null, 2)
    window.dispatchEvent(new Event('pantry:changed'))
    await loadLatest()
  } catch (e) { result.value = e.message }
}
</script>
