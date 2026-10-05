<template>
  <div>
    <h1>按临期消费</h1>
    <select v-model.number="item_id"><option v-for="i in items" :value="i.id">{{ i.name }}</option></select>
    <input type="number" v-model.number="qty" />
    <button @click="go">FEFO 扣减</button>
    <pre v-if="result">{{ result }}</pre>
    <ConsumeHistory ref="histRef" />
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import ConsumeHistory from '../components/ConsumeHistory.vue'
const items = ref([])
const item_id = ref(1)
const qty = ref(1)
const result = ref('')
const histRef = ref(null)
onMounted(async () => {
  items.value = await api('/items')
  if (items.value[0]) item_id.value = items.value[0].id
})
async function go() {
  try {
    result.value = JSON.stringify(await api('/consume', {
      method: 'POST', body: JSON.stringify({ item_id: item_id.value, qty: qty.value }),
    }), null, 2)
    // New deduction becomes the latest; refresh the history so the reversal
    // button moves onto it.
    await histRef.value?.reload()
    window.dispatchEvent(new CustomEvent('pantry-changed'))
  } catch (e) { result.value = e.message }
}
</script>
