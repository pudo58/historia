import React from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import AuthGate from './AuthGate';
import './studio.css';
import './workspace.css';

createRoot(document.getElementById('root')!).render(<React.StrictMode><QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:1,refetchOnWindowFocus:false}}})}><AuthGate><App/></AuthGate></QueryClientProvider></React.StrictMode>);
