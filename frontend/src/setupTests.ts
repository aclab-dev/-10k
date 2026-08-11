import '@testing-library/jest-dom/vitest'

// jsdom no implementa showModal()/close() de <dialog> (solo el elemento y el
// atributo `open`, no el comportamiento). KillSwitchButton depende de ambos
// para el focus trap y el cierre por Escape nativos — sin este polyfill,
// dialogRef.current?.showModal() explota en cualquier test que abra el modal.
//
// Esto solo habilita que los tests corran — NO valida comportamiento modal.
// El polyfill togglea el atributo `open` nada más: no hay focus trap, no hay
// cierre por Escape, y el listener de 'cancel' con preventDefault en
// KillSwitchButton nunca se ejercita acá. Justo las tres razones por las que
// se migró a <dialog> nativo no están cubiertas por esta suite (haría falta
// un runner con navegador real, no jsdom).
if (typeof HTMLDialogElement !== 'undefined' && !HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '')
  }
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.removeAttribute('open')
    this.dispatchEvent(new Event('close'))
  }
}
