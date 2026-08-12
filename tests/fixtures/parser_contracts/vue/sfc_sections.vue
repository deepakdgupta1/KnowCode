<template lang="html" data-view="contract">
  <section>{{ title }}</section>
</template>
<script lang='ts' setup>
const title = ref('Contract')
function select() {
  return title.value
}
</script>
<style scoped lang='css'>
.contract { color: v-bind(title); }
</style>
