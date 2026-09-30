import React from 'react';

export function Badge({children, good = false}: {children: React.ReactNode; good?: boolean}) { return <span className={`badge ${good ? 'good' : ''}`}>{children}</span>; }
export function Field({label, children}: {label: string; children: React.ReactNode}) { return <label className="field"><span>{label}</span>{children}</label>; }
export function Empty({title, children}: {title: string; children: React.ReactNode}) { return <div className="empty"><div className="empty-symbol">◇</div><h3>{title}</h3><p>{children}</p></div>; }
