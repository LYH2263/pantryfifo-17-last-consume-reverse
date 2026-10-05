import { createRouter, createWebHistory } from 'vue-router'
import Fridge from './pages/Fridge.vue'
import Layer from './pages/Layer.vue'
import Inbound from './pages/Inbound.vue'
import Consume from './pages/Consume.vue'
import History from './pages/History.vue'
import Settings from './pages/Settings.vue'
export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: Fridge },
    { path: '/layer/:layer', component: Layer, props: true },
    { path: '/inbound', component: Inbound },
    { path: '/consume', component: Consume },
    { path: '/history', component: History },
    { path: '/settings', component: Settings },
  ],
})
