<template>
  <button @click="increment">{{ count }}</button>
  <input v-model="count" />
</template>
<script>
export default {
  data() {
    return { count: 0 }
  },
  methods: {
    increment() {
      this.count++
    }
  }
}
</script>
