import '@testing-library/jest-dom/vitest'

// jsdom no implementa showModal()/close() de <dialog> (solo el elemento y el
// atributo `open`, no el comportamiento). KillSwitchButton depende de ambos
// para el focus trap y el cierre por Escape nativos — sin este polyfill,
// dialogRef.current?.showModal() explota en cualquier test que abra el modal.
if (typeof HTMLDialogElement !== 'undefined' && !HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '')
  }
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.removeAttribute('open')
    this.dispatchEvent(new Event('close'))
  }
}
