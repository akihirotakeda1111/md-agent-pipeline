package todo

import (
	"errors"
	"fmt"
	"sync"
	"testing"
)

func TestRepositoryCreateTrimsTitleAndDefaultsCompleted(t *testing.T) {
	repo := NewRepository()

	todo, err := repo.Create("  buy milk  ")
	if err != nil {
		t.Fatalf("Create returned error: %v", err)
	}

	if todo.ID != 1 {
		t.Fatalf("Create ID = %d, want 1", todo.ID)
	}
	if todo.Title != "buy milk" {
		t.Fatalf("Create Title = %q, want %q", todo.Title, "buy milk")
	}
	if todo.Completed {
		t.Fatalf("Create Completed = true, want false")
	}

	got, ok := repo.Get(todo.ID)
	if !ok {
		t.Fatalf("Get(%d) reported missing", todo.ID)
	}
	if got != todo {
		t.Fatalf("Get(%d) = %#v, want %#v", todo.ID, got, todo)
	}
}

func TestRepositoryCreateRejectsBlankTitle(t *testing.T) {
	repo := NewRepository()

	_, err := repo.Create(" \t\n ")
	if !errors.Is(err, ErrInvalidTitle) {
		t.Fatalf("Create blank title error = %v, want ErrInvalidTitle", err)
	}
}

func TestRepositoryListOrdersByAscendingID(t *testing.T) {
	repo := NewRepository()

	first, err := repo.Create("first")
	if err != nil {
		t.Fatalf("Create first returned error: %v", err)
	}
	second, err := repo.Create("second")
	if err != nil {
		t.Fatalf("Create second returned error: %v", err)
	}
	third, err := repo.Create("third")
	if err != nil {
		t.Fatalf("Create third returned error: %v", err)
	}

	if !repo.Delete(second.ID) {
		t.Fatalf("Delete(%d) = false, want true", second.ID)
	}

	list := repo.List()
	if len(list) != 2 {
		t.Fatalf("List length = %d, want 2", len(list))
	}
	if list[0].ID != first.ID || list[1].ID != third.ID {
		t.Fatalf("List IDs = [%d %d], want [%d %d]", list[0].ID, list[1].ID, first.ID, third.ID)
	}
}

func TestRepositoryUpdateAndDeleteDistinguishMissingIDs(t *testing.T) {
	repo := NewRepository()

	todo, err := repo.Create("task")
	if err != nil {
		t.Fatalf("Create returned error: %v", err)
	}

	updated, ok, err := repo.Update(todo.ID, "updated task", true)
	if err != nil {
		t.Fatalf("Update returned error: %v", err)
	}
	if !ok {
		t.Fatalf("Update(%d) reported missing", todo.ID)
	}
	if updated.Title != "updated task" || !updated.Completed {
		t.Fatalf("Update returned %#v, want updated title and completed=true", updated)
	}

	if _, ok := repo.Get(999); ok {
		t.Fatalf("Get(999) reported present, want missing")
	}

	if _, ok, err := repo.Update(999, "missing", false); err != nil || ok {
		t.Fatalf("Update(999) = ok=%v err=%v, want ok=false err=nil", ok, err)
	}

	if repo.Delete(999) {
		t.Fatalf("Delete(999) = true, want false")
	}

	if !repo.Delete(todo.ID) {
		t.Fatalf("Delete(%d) = false, want true", todo.ID)
	}
	if _, ok := repo.Get(todo.ID); ok {
		t.Fatalf("Get(%d) reported present after delete", todo.ID)
	}
}

func TestRepositoryConcurrentCreatesRemainDeterministic(t *testing.T) {
	repo := NewRepository()

	const total = 32

	errCh := make(chan error, total)
	var wg sync.WaitGroup
	wg.Add(total)
	for i := 0; i < total; i++ {
		i := i
		go func() {
			defer wg.Done()
			if _, err := repo.Create(fmt.Sprintf("task %d", i)); err != nil {
				errCh <- err
			}
		}()
	}
	wg.Wait()
	close(errCh)

	for err := range errCh {
		if err != nil {
			t.Fatalf("Create returned error: %v", err)
		}
	}

	list := repo.List()
	if len(list) != total {
		t.Fatalf("List length = %d, want %d", len(list), total)
	}
	for i, todo := range list {
		wantID := i + 1
		if todo.ID != wantID {
			t.Fatalf("List[%d].ID = %d, want %d", i, todo.ID, wantID)
		}
	}
}
