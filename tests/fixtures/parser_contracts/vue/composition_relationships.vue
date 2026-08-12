<template>
  <button @click="increment">{{ doubled }}</button>
  <input v-model="count" />
</template>
<script setup>
import { computed, ref } from 'vue'
const count = ref(0)
const doubled = computed(() => count.value * 2)
function increment() { count.value++ }
</script>
<style scoped>
.counter { width: v-bind(doubled); }
</style>
