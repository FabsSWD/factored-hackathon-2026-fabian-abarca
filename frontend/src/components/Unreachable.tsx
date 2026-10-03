// The one text the chat writes itself: without the backend there are no texts to show. It is
// bilingual on purpose and the only exception to M14's rule that texts come from the backend.
export function Unreachable({ onRetry }: { onRetry: () => void }) {
  return (
    <section className="card unreachable reveal" role="alert">
      <p lang="es">No pudimos conectar con el servicio.</p>
      <p lang="pt">Não conseguimos conectar com o serviço.</p>
      <button type="button" className="button button--copper" onClick={onRetry}>
        <span lang="es">Reintentar</span> · <span lang="pt">Tentar de novo</span>
      </button>
    </section>
  );
}
