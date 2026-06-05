import WhiteboardCanvas from './WhiteboardCanvas'
import OpenPM from '../OpenPM/OpenPM'
import Timeline from '../Timeline/Timeline'

export default function Whiteboard({
  activeTool, setActiveTool,
  activeView, setActiveView,
  canvasState, canvasDispatch,
  onLoadSelfMap,
  onGenerateFromRepo,
  onExecute,
  executing,
  // In-place nesting (Step 16)
  archGraph,
  expandedIds,
  onToggleExpand,
  onSelectArchEntity,
  archSel,
  onExpandModule,
  flowMode = 'story',
  story = null,
  runNodeStates,
  runEdgeStates,
  tetherSpotlight = null,
  onClearTetherSpotlight,
  gitCommits,
  onSelectCommit,
  // PM props
  tasks, pmDispatch,
  designEvents, onTaskEvent,
  selectedTaskId, setSelectedTaskId,
  setPanelMode,
}) {
  if (activeView === 'timeline') {
    return (
      <Timeline
        designEvents={designEvents}
        canvasState={canvasState}
        canvasDispatch={canvasDispatch}
        setActiveView={setActiveView}
        setPanelMode={setPanelMode}
        tasks={tasks}
        setSelectedTaskId={setSelectedTaskId}
        gitCommits={gitCommits}
        onSelectCommit={onSelectCommit}
      />
    )
  }

  if (activeView === 'pm') {
    return (
      <OpenPM
        tasks={tasks}
        pmDispatch={pmDispatch}
        canvasState={canvasState}
        canvasDispatch={canvasDispatch}
        setActiveView={setActiveView}
        setPanelMode={setPanelMode}
        selectedTaskId={selectedTaskId}
        setSelectedTaskId={setSelectedTaskId}
        onTaskEvent={onTaskEvent}
      />
    )
  }

  return (
    <div className="wb-shell">
      <div className="arch-header">
        <span className="arch-header-title">openArchitect</span>
        {/* Story is the default, selection-aware presentation. Flow modes and
            Expand/Collapse now live in the Technical right panel (Step 28 Slice 4). */}
      </div>
      <div className="wb-body">
        <WhiteboardCanvas
          activeTool={activeTool}
          setActiveTool={setActiveTool}
          state={canvasState}
          dispatch={canvasDispatch}
          onLoadSelfMap={onLoadSelfMap}
          onGenerateFromRepo={onGenerateFromRepo}
          onExecute={onExecute}
          executing={executing}
          archGraph={archGraph}
          expandedIds={expandedIds}
          onToggleExpand={onToggleExpand}
          onSelectArchEntity={onSelectArchEntity}
          archSel={archSel}
          onExpandModule={onExpandModule}
          flowMode={flowMode}
          story={story}
          runNodeStates={runNodeStates}
          runEdgeStates={runEdgeStates}
          tetherSpotlight={tetherSpotlight}
          onClearTetherSpotlight={onClearTetherSpotlight}
        />
      </div>
    </div>
  )
}
