<template>
  <button @click="onClick">{{ label }}</button>
</template>

<script setup>
import { ref } from 'vue'

const label = ref('hi')

function onClick() {
  label.value = 'bye'
}
</script>
