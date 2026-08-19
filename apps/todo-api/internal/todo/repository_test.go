package todo

import (
	"fmt"
	"sync"
	"testing"
)

func TestRepositoryCreateTrimsTitleAndDefaultsCompleted(t *testing.T) {
	repo := NewRepository()

	item, err := repo.Create("  buy milk  ")
	if err != nil {
		t.Fatalf("Create returned error: %v", err)
	}
	if item.ID != 1 {
		t.Fatalf("ID = %d, want 1", item.ID)
	}
	if item.Title != "buy milk" {
		t.Fatalf("Title = %q, want %q", item.Title, "buy milk")
	}
	if item.Completed {
		t.Fatalf("Completed = true, want false")
	}

	got, ok := repo.Get(item.ID)
	if !ok {
		t.Fatalf("Get(%d) reported missing item", item.ID)
	}
	if got != item {
		t.Fatalf("Get(%d) = %+v, want %+v", item.ID, got, item)
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

	repo.Delete(second.ID)

	items := repo.List()
	if len(items) != 2 {
		t.Fatalf("List length = %d, want 2", len(items))
	}
	if items[0].ID != first.ID || items[1].ID != third.ID {
		t.Fatalf("List IDs = [%d %d], want [%d %d]", items[0].ID, items[1].ID, first.ID, third.ID)
	}
}

func TestRepositoryUpdateAndDeleteDistinguishMissingIDs(t *testing.T) {
	repo := NewRepository()

	if _, ok := repo.Get(99); ok {
		t.Fatalf("Get(99) unexpectedly found a todo")
	}
	if _, ok, err := repo.Update(99, "missing", true); err != nil || ok {
		t.Fatalf("Update(99, ...) = ok:%v err:%v, want ok:false err:nil", ok, err)
	}
	if repo.Delete(99) {
		t.Fatalf("Delete(99) unexpectedly reported success")
	}
}

func TestRepositoryConcurrentCreatesRemainConsistent(t *testing.T) {
	repo := NewRepository()

	const total = 32
	var wg sync.WaitGroup
	wg.Add(total)

	for i := 0; i < total; i++ {
		i := i
		go func() {
			defer wg.Done()
			if _, err := repo.Create(fmt.Sprintf("todo-%02d", i)); err != nil {
				t.Errorf("Create(%d) returned error: %v", i, err)
			}
		}()
	}

	wg.Wait()

	items := repo.List()
	if len(items) != total {
		t.Fatalf("List length = %d, want %d", len(items), total)
	}
	for i, item := range items {
		wantID := i + 1
		if item.ID != wantID {
			t.Fatalf("List[%d].ID = %d, want %d", i, item.ID, wantID)
		}
		if item.Title == "" {
			t.Fatalf("List[%d].Title is empty", i)
		}
	}
}

