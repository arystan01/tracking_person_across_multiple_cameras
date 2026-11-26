import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import './styles.css'
import './console.css'
import './signin.css'

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
