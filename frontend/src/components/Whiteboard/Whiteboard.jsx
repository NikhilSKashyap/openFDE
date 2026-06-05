import WhiteboardCanvas from './WhiteboardCanvas'
import OpenPM from '../OpenPM/OpenPM'
import Timeline from '../Timeline/Timeline'
import CommitChipRail from './CommitChipRail'

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
  watchBoxIds = null,
  spotlight = null,
  onClearSpotlight,
  onSpotlightCommit,
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
        {/* Canvas-native commit lens (Step 37a Slice 3) — click a chip to see
            what changed; never replaces the Timeline tab. */}
        <CommitChipRail
          commits={gitCommits}
          activeSha={spotlight?.kind === 'commit' ? spotlight.sha : null}
          onSpotlightCommit={onSpotlightCommit}
        />
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
          watchBoxIds={watchBoxIds}
          spotlight={spotlight}
          onClearSpotlight={onClearSpotlight}
        />
      </div>
    </div>
  )
}
