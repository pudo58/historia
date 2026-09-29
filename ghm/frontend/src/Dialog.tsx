import { useEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

type Props = {
  title: string;
  description?: string;
  children: ReactNode;
  onClose: () => void;
  className?: string;
};

/** One reusable, keyboard-accessible overlay for job journals and event details. */
export default function Dialog({ title, description, children, onClose, className = '' }: Props) {
  const headingId = useId();
  const descriptionId = useId();
  const panel = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const callback = useRef(onClose);
  callback.current = onClose;

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    close.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      const dialogs = document.querySelectorAll('[role="dialog"]');
      if (dialogs[dialogs.length - 1] !== panel.current) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        callback.current();
      } else if (event.key === 'Tab') {
        const focusable = Array.from(panel.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'
        ) || []).filter(node => node.getClientRects().length > 0);
        if (!focusable.length) { event.preventDefault(); panel.current?.focus(); return; }
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
          event.preventDefault(); last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault(); first.focus();
        }
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, []);

  return createPortal(
    <div className="studio-dialog-backdrop" onMouseDown={event => {
      if (event.target === event.currentTarget) callback.current();
    }}>
      <div ref={panel} className={`studio-dialog ${className}`} role="dialog" aria-modal="true"
        aria-labelledby={headingId} aria-describedby={description ? descriptionId : undefined} tabIndex={-1}>
        <header className="studio-dialog-header">
          <div><span className="work-kicker">HISTORIA / NHẬT KÝ</span><h2 id={headingId}>{title}</h2>
            {description && <p id={descriptionId}>{description}</p>}</div>
          <button ref={close} type="button" onClick={() => callback.current()} aria-label="Đóng popup" className="studio-dialog-close">Đóng ×</button>
        </header>
        <div className="studio-dialog-content">{children}</div>
      </div>
    </div>, document.body
  );
}
