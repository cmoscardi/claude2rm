--[[
  Pandoc Lua filter for the reMarkable build.

  Three jobs:
    1. Route display math through \rmkmath (shrink-to-fit + warn). Writes a
       sidecar "<n>\t<snippet>" map so `make lint` can turn the LaTeX warning
       "RMKWIDE eq 7" back into the actual equation source.
    2. Render ```mermaid blocks to PDF via mmdc, with a content-hash cache so
       unchanged diagrams are not re-rendered.
    3. Warn on raw HTML blocks, which pandoc otherwise drops silently when
       targeting LaTeX.
--]]

local eq_index = 0
local eq_map = {}

-- Display-math environments that must not be wrapped in $...$: pandoc passes
-- these through verbatim and they carry their own display semantics.
local BLOCK_ENVS = {
  'align', 'align%*', 'gather', 'gather%*', 'multline', 'multline%*',
  'equation', 'equation%*', 'eqnarray', 'flalign', 'alignat', 'split',
}

local function is_block_env(text)
  for _, env in ipairs(BLOCK_ENVS) do
    if text:match('^%s*\\begin{' .. env .. '}') then return true end
  end
  return false
end

function Math(el)
  if el.mathtype ~= 'DisplayMath' then return nil end

  -- Leave self-contained display environments alone; wrapping them in
  -- $\displaystyle ...$ would be a syntax error.
  if is_block_env(el.text) then return nil end

  eq_index = eq_index + 1
  local snippet = el.text:gsub('%s+', ' '):gsub('^%s', ''):sub(1, 100)
  table.insert(eq_map, string.format('%d\t%s', eq_index, snippet))

  return pandoc.RawInline('latex', '\\[\\rmkmath{' .. el.text .. '}\\]')
end

-- --------------------------------------------------------------------------
-- Mermaid
-- --------------------------------------------------------------------------

local function file_exists(path)
  local f = io.open(path, 'r')
  if f then f:close(); return true end
  return false
end

local mermaid_available = nil
local function have_mmdc()
  if mermaid_available == nil then
    mermaid_available = os.execute('command -v mmdc >/dev/null 2>&1') and true or false
  end
  return mermaid_available
end

function CodeBlock(el)
  if not el.classes:includes('mermaid') then return nil end

  local cache_dir = os.getenv('RMK_MERMAID_CACHE') or 'build/.cache/mermaid'
  local hash = pandoc.utils.sha1(el.text)
  local pdf = cache_dir .. '/' .. hash .. '.pdf'

  if not file_exists(pdf) then
    if not have_mmdc() then
      io.stderr:write('RMKMERMAID: mmdc not found; leaving diagram as a code block. ' ..
                      'Install with: npm install -g @mermaid-js/mermaid-cli\n')
      return nil
    end
    os.execute('mkdir -p ' .. cache_dir)
    local src = cache_dir .. '/' .. hash .. '.mmd'
    local fh = io.open(src, 'w')
    fh:write(el.text)
    fh:close()
    -- --pdfFit crops the page to the diagram so it scales sensibly in LaTeX.
    local cmd = string.format(
      'mmdc --input %s --output %s --pdfFit --backgroundColor white >/dev/null 2>&1',
      src, pdf)
    if not os.execute(cmd) then
      io.stderr:write('RMKMERMAID: mmdc failed to render a diagram; left as code block.\n')
      return nil
    end
  end

  -- width=\linewidth keeps wide diagrams inside the narrow page.
  local img = pandoc.Image({}, pdf, '', pandoc.Attr('', {}, {{'width', '\\linewidth'}}))
  return pandoc.Para({img})
end

-- --------------------------------------------------------------------------
-- Raw HTML: warn rather than drop silently
-- --------------------------------------------------------------------------

function RawBlock(el)
  if el.format == 'html' then
    local preview = el.text:gsub('%s+', ' '):sub(1, 70)
    io.stderr:write('RMKHTML: raw HTML block will not appear in the PDF: ' .. preview .. '\n')
  end
  return nil
end

-- --------------------------------------------------------------------------
-- Emit the equation sidecar map
-- --------------------------------------------------------------------------

function Pandoc(doc)
  local path = os.getenv('RMK_EQMAP')
  if path and #eq_map > 0 then
    local fh = io.open(path, 'w')
    if fh then
      fh:write(table.concat(eq_map, '\n') .. '\n')
      fh:close()
    end
  end
  return doc
end
